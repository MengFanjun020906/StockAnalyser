# -*- coding: utf-8 -*-
"""Base class for v2 candidate experts.

Each expert runs an independent bounded LLM tool-calling loop:
- system prompt is dimension-specific (in src.agent.candidate_experts_v2.prompts)
- only tools in ``allowed_tools`` are exposed; any LLM tool_call referencing a
  non-whitelisted tool is rejected with a diagnostic and does NOT execute.
- ``max_llm_rounds`` and ``max_tool_calls`` hard-cap the loop.
- Final output MUST be a JSON object matching ExpertPacketV2.candidates schema;
  experts that emit candidates without any tool_call invocations are marked
  ``partial`` (or ``invalid`` if completely empty).

The loop is intentionally implemented here (not via src.agent.runner) so this
package is self-contained and can be iterated independently of the legacy
analysis loop.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.agent.candidate_experts_v2.cache import (
    cache_key,
    load_packet,
    save_packet,
    seed_hash,
)
from src.agent.candidate_experts_v2.schemas import (
    EvidenceItem,
    ExpertCandidateV2,
    ExpertDataQualityV2,
    ExpertPacketV2,
    RiskNote,
    SeedItem,
    SeedSummaryV2,
)

logger = logging.getLogger(__name__)


ToolFn = Callable[..., Any]


@dataclass
class LLMToolCall:
    """One LLM-requested tool call to be handled by the bounded loop."""

    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


@dataclass
class LLMTurn:
    """One LLM response turn: either tool calls or a final text payload."""

    tool_calls: List[LLMToolCall] = field(default_factory=list)
    text: str = ""


LLMCallable = Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], LLMTurn]
"""Signature for the LLM driver injected into a BaseExpert.

Args:
    messages: OpenAI-style chat messages (system/user/assistant/tool).
    tool_decls: Tool declarations in OpenAI function-calling format.

Returns:
    LLMTurn describing the model's response.
"""


def _safe_json_loads(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    # tolerate ```json fences
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


class BaseExpert:
    """Bounded tool-calling expert. Subclasses define dimension/prompt/tools."""

    expert_name: str = "base_expert"
    dimension: str = "candidate"

    def __init__(
        self,
        *,
        allowed_tools: Sequence[str],
        tool_registry: Dict[str, ToolFn],
        tool_decls: Sequence[Dict[str, Any]],
        llm: LLMCallable,
        system_prompt: str,
        max_llm_rounds: int = 3,
        max_tool_calls: int = 6,
        freshness: str = "intraday",
    ) -> None:
        self.allowed_tools = set(allowed_tools)
        self.tool_registry = tool_registry
        self.tool_decls = [
            decl for decl in tool_decls if _decl_name(decl) in self.allowed_tools
        ]
        self.llm = llm
        self.system_prompt = system_prompt
        self.max_llm_rounds = max(1, int(max_llm_rounds))
        self.max_tool_calls = max(1, int(max_tool_calls))
        self.freshness = freshness

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        seed_pool: Sequence[SeedItem],
        *,
        market: str = "cn",
        use_cache: bool = True,
    ) -> ExpertPacketV2:
        started = time.time()
        seeds_h = seed_hash(seed_pool)
        key = cache_key(self.expert_name, market, seeds_h)
        if use_cache:
            cached = load_packet(key, dimension=self.dimension)
            if cached is not None:
                cached.elapsed_ms = int((time.time() - started) * 1000)
                return cached

        packet = self._run_uncached(list(seed_pool), market=market)
        packet.elapsed_ms = int((time.time() - started) * 1000)
        if use_cache and packet.status in {"ok", "partial"}:
            try:
                save_packet(key, packet, dimension=self.dimension)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("cache write failed for %s: %s", key, exc)
        return packet

    # ------------------------------------------------------------------
    # Bounded tool-calling loop
    # ------------------------------------------------------------------

    def _run_uncached(self, seeds: List[SeedItem], *, market: str) -> ExpertPacketV2:
        user_message = self._build_user_message(seeds, market=market)
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_message},
        ]

        diagnostics: List[Dict[str, Any]] = []
        tool_call_log: List[Dict[str, Any]] = []
        errors: List[str] = []
        tool_calls_executed = 0
        used_tools_any = False

        for round_idx in range(self.max_llm_rounds):
            try:
                turn = self.llm(messages, list(self.tool_decls))
            except Exception as exc:
                errors.append(f"llm_call_failed_round_{round_idx}: {exc}")
                return self._packet(
                    seeds=seeds,
                    status="failed",
                    candidates=[],
                    rejected=[],
                    diagnostics=diagnostics,
                    tool_calls=tool_call_log,
                    errors=errors,
                )

            if turn.tool_calls:
                # Append assistant message describing the tool calls
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call.call_id or f"call_{round_idx}_{idx}",
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                                },
                            }
                            for idx, call in enumerate(turn.tool_calls)
                        ],
                    }
                )

                for call in turn.tool_calls:
                    if tool_calls_executed >= self.max_tool_calls:
                        diagnostics.append(
                            {
                                "source": "bounded_loop",
                                "status": "tool_call_cap_reached",
                                "max_tool_calls": self.max_tool_calls,
                            }
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.call_id or "cap",
                                "name": call.name,
                                "content": json.dumps(
                                    {"status": "error", "reason": "max_tool_calls reached"},
                                    ensure_ascii=False,
                                ),
                            }
                        )
                        continue

                    if call.name not in self.allowed_tools:
                        diagnostics.append(
                            {
                                "source": "bounded_loop",
                                "status": "tool_not_whitelisted",
                                "tool": call.name,
                            }
                        )
                        tool_call_log.append(
                            {"tool": call.name, "status": "rejected", "reason": "not_whitelisted"}
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.call_id or call.name,
                                "name": call.name,
                                "content": json.dumps(
                                    {
                                        "status": "error",
                                        "reason": f"tool {call.name} not in whitelist for {self.expert_name}",
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        )
                        continue

                    fn = self.tool_registry.get(call.name)
                    if fn is None:
                        diagnostics.append(
                            {
                                "source": "bounded_loop",
                                "status": "tool_unavailable",
                                "tool": call.name,
                            }
                        )
                        tool_call_log.append(
                            {"tool": call.name, "status": "unavailable"}
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.call_id or call.name,
                                "name": call.name,
                                "content": json.dumps(
                                    {"status": "error", "reason": "tool not provided"},
                                    ensure_ascii=False,
                                ),
                            }
                        )
                        continue

                    tool_calls_executed += 1
                    used_tools_any = True
                    call_started = time.time()
                    try:
                        tool_result = fn(**(call.arguments or {}))
                        result_payload = _coerce_tool_result(tool_result)
                        tool_call_log.append(
                            {
                                "tool": call.name,
                                "status": "ok",
                                "elapsed_ms": int((time.time() - call_started) * 1000),
                            }
                        )
                    except Exception as exc:
                        result_payload = {"status": "error", "reason": str(exc)}
                        tool_call_log.append(
                            {
                                "tool": call.name,
                                "status": "failed",
                                "error": str(exc),
                                "elapsed_ms": int((time.time() - call_started) * 1000),
                            }
                        )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id or call.name,
                            "name": call.name,
                            "content": json.dumps(result_payload, ensure_ascii=False, default=str),
                        }
                    )
                continue  # let LLM see tool results in next round

            # No tool calls -> treat as final answer
            parsed = _safe_json_loads(turn.text)
            if parsed is None:
                errors.append("final_output_not_json")
                return self._packet(
                    seeds=seeds,
                    status="failed",
                    candidates=[],
                    rejected=[],
                    diagnostics=diagnostics,
                    tool_calls=tool_call_log,
                    errors=errors,
                )

            candidates = self._parse_candidates(parsed)
            rejected = parsed.get("rejected") if isinstance(parsed, dict) else None
            data_quality = self._parse_data_quality(parsed)
            if not used_tools_any:
                # Per design doc §4: no-tool output is "partial" (with candidates)
                # or "empty" (without). packet.status enum has no "invalid"
                # — that value lives on candidate.stance instead.
                status = "partial" if candidates else "empty"
                diagnostics.append(
                    {
                        "source": "bounded_loop",
                        "status": "no_tool_calls",
                        "note": "Expert produced output without invoking whitelisted tools.",
                    }
                )
            else:
                status = "ok" if candidates else "empty"
            return ExpertPacketV2(
                expert=self.expert_name,
                dimension=self.dimension,
                status=status,
                seed_summary=self._seed_summary(
                    seeds,
                    accepted_count=len(candidates),
                    rejected_count=len(rejected) if isinstance(rejected, list) else 0,
                ),
                data_quality=data_quality,
                candidates=candidates,
                rejected=list(rejected) if isinstance(rejected, list) else [],
                tool_calls=tool_call_log,
                diagnostics=diagnostics,
                errors=errors,
            )

        # Reached max_llm_rounds without a final answer
        diagnostics.append(
            {
                "source": "bounded_loop",
                "status": "round_cap_reached",
                "max_llm_rounds": self.max_llm_rounds,
            }
        )
        return self._packet(
            seeds=seeds,
            status="partial",
            candidates=[],
            rejected=[],
            diagnostics=diagnostics,
            tool_calls=tool_call_log,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Hooks subclasses may override
    # ------------------------------------------------------------------

    def _build_user_message(self, seeds: Sequence[SeedItem], *, market: str) -> str:
        seed_payload = [
            {
                "code": seed.code,
                "name": seed.name,
                "source": seed.source,
                "hint": seed.hint,
            }
            for seed in seeds[:30]
        ]
        return (
            f"市场: {market}\n"
            f"维度: {self.dimension}\n"
            f"专家: {self.expert_name}\n"
            f"候选种子池（最多 30 条，按你的工具白名单筛选/解释，不要发现新代码）:\n"
            f"{json.dumps(seed_payload, ensure_ascii=False)}\n\n"
            "请按 system prompt 的要求调用工具、给出证据，并以 JSON 输出最终候选。"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _packet(
        self,
        *,
        seeds: Sequence[SeedItem],
        status: str,
        candidates: List[ExpertCandidateV2],
        rejected: List[Dict[str, Any]],
        diagnostics: List[Dict[str, Any]],
        tool_calls: List[Dict[str, Any]],
        errors: List[str],
    ) -> ExpertPacketV2:
        return ExpertPacketV2(
            expert=self.expert_name,
            dimension=self.dimension,
            status=status,  # type: ignore[arg-type]
            seed_summary=self._seed_summary(
                seeds,
                accepted_count=len(candidates),
                rejected_count=len(rejected),
            ),
            data_quality=ExpertDataQualityV2(freshness=self.freshness),
            candidates=candidates,
            rejected=rejected,
            tool_calls=tool_calls,
            diagnostics=diagnostics,
            errors=errors,
        )

    def _seed_summary(
        self,
        seeds: Sequence[SeedItem],
        *,
        accepted_count: int,
        rejected_count: int,
    ) -> SeedSummaryV2:
        source_counts: Dict[str, int] = {}
        for seed in seeds:
            source_counts[seed.source] = source_counts.get(seed.source, 0) + 1
        return SeedSummaryV2(
            seed_count=len(seeds),
            accepted_count=accepted_count,
            rejected_count=rejected_count,
            seed_sources=source_counts,
        )

    def _parse_candidates(self, payload: Dict[str, Any]) -> List[ExpertCandidateV2]:
        raw = payload.get("candidates") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            return []
        parsed: List[ExpertCandidateV2] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            evidence_raw = item.get("evidence") or []
            evidence: List[EvidenceItem] = []
            if isinstance(evidence_raw, list):
                for ev in evidence_raw:
                    if not isinstance(ev, dict):
                        continue
                    tool_name = str(ev.get("tool") or "").strip()
                    if not tool_name:
                        continue
                    evidence.append(
                        EvidenceItem(
                            tool=tool_name,
                            summary=str(ev.get("summary") or ""),
                            metrics=ev.get("metrics") if isinstance(ev.get("metrics"), dict) else {},
                        )
                    )
            risks_raw = item.get("risks") or []
            risks: List[RiskNote] = []
            if isinstance(risks_raw, list):
                for risk in risks_raw:
                    if not isinstance(risk, dict):
                        continue
                    risks.append(
                        RiskNote(
                            type=str(risk.get("type") or "risk"),
                            summary=str(risk.get("summary") or ""),
                        )
                    )
            try:
                parsed.append(
                    ExpertCandidateV2(
                        code=code,
                        name=str(item.get("name") or code),
                        market=str(item.get("market") or "cn"),
                        score=float(item.get("score") or 50.0),
                        confidence=float(item.get("confidence") or 0.5),
                        stance=str(item.get("stance") or "support"),  # type: ignore[arg-type]
                        setup_type=str(item.get("setup_type") or item.get("candidate_type") or "") or None,  # type: ignore[arg-type]
                        reason=str(item.get("reason") or ""),
                        evidence=evidence,
                        risks=risks,
                        valid_until=str(item.get("valid_until") or "next_trading_day"),
                    )
                )
            except Exception as exc:
                logger.debug("skip malformed candidate %s: %s", item, exc)
        return parsed

    def _parse_data_quality(self, payload: Dict[str, Any]) -> ExpertDataQualityV2:
        dq_raw = payload.get("data_quality") if isinstance(payload, dict) else None
        if not isinstance(dq_raw, dict):
            return ExpertDataQualityV2(freshness=self.freshness)
        return ExpertDataQualityV2(
            freshness=str(dq_raw.get("freshness") or self.freshness),
            as_of=dq_raw.get("as_of"),
            source_chain=[str(item) for item in (dq_raw.get("source_chain") or []) if item],
            warnings=[str(item) for item in (dq_raw.get("warnings") or []) if item],
        )


def _decl_name(decl: Dict[str, Any]) -> str:
    if not isinstance(decl, dict):
        return ""
    fn = decl.get("function") if isinstance(decl.get("function"), dict) else None
    if fn and fn.get("name"):
        return str(fn["name"])
    return str(decl.get("name") or "")


def _coerce_tool_result(value: Any) -> Any:
    """Make a tool result JSON-serializable for the LLM message log."""
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        return value
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return str(value)
