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


FINAL_JSON_REMINDER = (
    "你已经收到工具结果。现在停止调用工具，只输出一个合法 JSON object；"
    "不要输出 Markdown、解释、代码块或自然语言前后缀。"
    "顶层只能包含 data_quality、candidates、rejected 三个字段；"
    "单只股票结论必须放进 candidates 或 rejected 数组元素里，禁止把单只股票对象直接放在顶层。"
    "示例 JSON："
    '{"data_quality":{"freshness":"intraday","warnings":[]},"candidates":[],"rejected":[]}'
)

FINAL_JSON_RESPONSE_FORMAT = {"type": "json_object"}
FINAL_JSON_MAX_TOKENS = 8192


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
    usage: Dict[str, Any] = field(default_factory=dict)


LLMCallable = Callable[..., LLMTurn]
"""Signature for the LLM driver injected into a BaseExpert.

Args:
    messages: OpenAI-style chat messages (system/user/assistant/tool).
    tool_decls: Tool declarations in OpenAI function-calling format.

Returns:
    LLMTurn describing the model's response.
"""


def _call_llm_driver(
    llm: LLMCallable,
    messages: List[Dict[str, Any]],
    tool_decls: List[Dict[str, Any]],
    *,
    response_format: Optional[Dict[str, Any]] = None,
    max_tokens: Optional[int] = None,
) -> LLMTurn:
    """Call an LLM driver with optional structured-output kwargs.

    Existing tests and custom drivers may still implement the older
    ``(messages, tool_decls)`` signature, so kwargs are best-effort.
    """

    kwargs: Dict[str, Any] = {}
    if response_format is not None:
        kwargs["response_format"] = response_format
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if kwargs:
        try:
            return llm(messages, tool_decls, **kwargs)
        except TypeError as exc:
            msg = str(exc)
            if "unexpected keyword" not in msg and "positional" not in msg:
                raise
    return llm(messages, tool_decls)


def _should_force_json_output(messages: Sequence[Dict[str, Any]]) -> bool:
    if not messages:
        return False
    last = messages[-1]
    content = str(last.get("content") or "").lower()
    return "json" in content and "object" in content


def _registry_lookup(tool_registry: Any, name: str) -> Any:
    """Best-effort lookup that works for both ToolRegistry and plain dict."""
    if hasattr(tool_registry, "get"):
        try:
            return tool_registry.get(name)
        except TypeError:
            pass
    if isinstance(tool_registry, dict):
        return tool_registry.get(name)
    return None


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
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(cleaned[start : end + 1])
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
        return None


def _completion_tokens(usage: Optional[Dict[str, Any]]) -> Optional[int]:
    if not isinstance(usage, dict):
        return None
    value = usage.get("completion_tokens") or usage.get("output_tokens")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _preview_text(text: str, limit: int = 1200) -> str:
    cleaned = str(text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "...[truncated]"


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
        self._progress_events: Optional[List[Dict[str, Any]]] = None

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
                llm_started = time.time()
                tool_decls = list(self.tool_decls)
                final_json_turn = (not tool_decls) or _should_force_json_output(messages)
                turn = _call_llm_driver(
                    self.llm,
                    messages,
                    tool_decls,
                    response_format=FINAL_JSON_RESPONSE_FORMAT if final_json_turn else None,
                    max_tokens=FINAL_JSON_MAX_TOKENS if final_json_turn else None,
                )
                self._record_progress_event(
                    {
                        "source": "bounded_loop_progress",
                        "phase": "llm_turn",
                        "status": "tool_calls" if turn.tool_calls else "final_text",
                        "round": round_idx,
                        "tool_call_count": len(turn.tool_calls or []),
                        "tools": [call.name for call in (turn.tool_calls or [])],
                        "text_chars": len(turn.text or ""),
                        "elapsed_ms": int((time.time() - llm_started) * 1000),
                    }
                )
            except Exception as exc:
                errors.append(f"llm_call_failed_round_{round_idx}: {exc}")
                self._record_progress_event(
                    {
                        "source": "bounded_loop_progress",
                        "phase": "llm_turn",
                        "status": "failed",
                        "round": round_idx,
                        "error": str(exc),
                    }
                )
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
                        self._record_progress_event(
                            {
                                "source": "bounded_loop_progress",
                                "phase": "tool_call_skipped",
                                "status": "tool_call_cap_reached",
                                "round": round_idx,
                                "tool": call.name,
                                "call_id": call.call_id,
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
                        self._record_progress_event(
                            {
                                "source": "bounded_loop_progress",
                                "phase": "tool_call_skipped",
                                "status": "tool_not_whitelisted",
                                "round": round_idx,
                                "tool": call.name,
                                "call_id": call.call_id,
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
                        self._record_progress_event(
                            {
                                "source": "bounded_loop_progress",
                                "phase": "tool_call_skipped",
                                "status": "tool_unavailable",
                                "round": round_idx,
                                "tool": call.name,
                                "call_id": call.call_id,
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
                    self._record_progress_event(
                        {
                            "source": "bounded_loop_progress",
                            "phase": "tool_call_start",
                            "status": "started",
                            "round": round_idx,
                            "tool": call.name,
                            "call_id": call.call_id,
                            "arguments": _compact_progress_args(call.arguments),
                        }
                    )
                    try:
                        tool_result = fn(**(call.arguments or {}))
                        result_payload = _coerce_tool_result(tool_result)
                        elapsed_ms = int((time.time() - call_started) * 1000)
                        tool_call_log.append(
                            {
                                "tool": call.name,
                                "status": "ok",
                                "elapsed_ms": elapsed_ms,
                            }
                        )
                        self._record_progress_event(
                            {
                                "source": "bounded_loop_progress",
                                "phase": "tool_call_end",
                                "status": "ok",
                                "round": round_idx,
                                "tool": call.name,
                                "call_id": call.call_id,
                                "elapsed_ms": elapsed_ms,
                            }
                        )
                    except Exception as exc:
                        result_payload = {"status": "error", "reason": str(exc)}
                        elapsed_ms = int((time.time() - call_started) * 1000)
                        tool_call_log.append(
                            {
                                "tool": call.name,
                                "status": "failed",
                                "error": str(exc),
                                "elapsed_ms": elapsed_ms,
                            }
                        )
                        self._record_progress_event(
                            {
                                "source": "bounded_loop_progress",
                                "phase": "tool_call_end",
                                "status": "failed",
                                "round": round_idx,
                                "tool": call.name,
                                "call_id": call.call_id,
                                "error": str(exc),
                                "elapsed_ms": elapsed_ms,
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
                messages.append({"role": "user", "content": FINAL_JSON_REMINDER})
                continue  # let LLM see tool results in next round

            # No tool calls -> treat as final answer
            parsed = _safe_json_loads(turn.text)
            if parsed is None:
                errors.append("final_output_not_json")
                if not str(turn.text or "").strip():
                    errors.append("final_output_empty_content")
                completion_tokens = _completion_tokens(turn.usage)
                if completion_tokens is not None and completion_tokens >= FINAL_JSON_MAX_TOKENS:
                    errors.append("final_output_maybe_truncated")
                diagnostics.append(
                    {
                        "source": "bounded_loop",
                        "status": "final_output_not_json",
                        "text_chars": len(turn.text or ""),
                        "text_preview": _preview_text(turn.text),
                        "completion_tokens": completion_tokens,
                        "max_tokens": FINAL_JSON_MAX_TOKENS,
                    }
                )
                return self._packet(
                    seeds=seeds,
                    status="failed",
                    candidates=[],
                    rejected=[
                        {
                            "code": seed.code,
                            "name": seed.name,
                            "reason": "final_output_not_json",
                            "evidence": [
                                {
                                    "tool": "llm_final_output",
                                    "summary": _preview_text(turn.text) or "empty final output",
                                }
                            ],
                        }
                        for seed in seeds
                    ],
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
                "priority_score": seed.priority_score,
                "freshness": seed.freshness,
                "trigger_signals": seed.trigger_signals[:5],
                "context_hint": seed.context_hint,
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

    def _record_progress_event(self, event: Dict[str, Any]) -> None:
        """Expose in-flight LLM/tool progress to outer timeout guards."""

        events = getattr(self, "_progress_events", None)
        if not isinstance(events, list):
            return
        payload = dict(event)
        payload.setdefault("ts_ms", int(time.time() * 1000))
        events.append(payload)

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

    def _compute_confidence_from_tool_coverage(self, candidate: "ExpertCandidateV2") -> float:
        """根据证据工具覆盖率计算置信度，不依赖 LLM 猜测的数字。"""
        evidence = candidate.evidence or []
        if not evidence:
            return 0.3
        tools_used = len({ev.tool for ev in evidence if ev.tool})
        base = min(0.9, 0.5 + tools_used * 0.15)
        empty_summary = sum(1 for ev in evidence if not ev.summary)
        penalty = empty_summary * 0.05
        return round(max(0.2, base - penalty), 2)

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
                        score=float(item.get("score") if isinstance(item.get("score"), (int, float)) else 50.0),
                        confidence=float(item.get("confidence") if isinstance(item.get("confidence"), (int, float)) else 0.5),
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
        # 用工具覆盖率覆盖 LLM 生成的 confidence（LLM 没有全市场对比数据，生成的数字无依据）
        for cand in parsed:
            cand.confidence = self._compute_confidence_from_tool_coverage(cand)
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


def _compact_progress_args(arguments: Dict[str, Any]) -> Dict[str, Any]:
    compact: Dict[str, Any] = {}
    for key in ("stock_code", "symbol", "code", "market", "period", "days", "timeout_seconds"):
        if key in (arguments or {}):
            value = arguments.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                compact[key] = value
            else:
                compact[key] = str(value)[:120]
    return compact


def _coerce_tool_result(value: Any) -> Any:
    """Make a tool result JSON-serializable for the LLM message log."""
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        return value
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return str(value)
