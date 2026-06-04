#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Probe thesis-desk execution from a saved Agent trace.

This script reuses the seed preview saved in a trace and runs the thesis
desks without rebuilding the seed pool.  It is intended for isolating whether a
stall happens before the first LLM turn, inside the desk loop, or after a tool
call.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency in some envs
    load_dotenv = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.candidate_experts_v2.committee import SeedPoolBuildResult
from src.agent.candidate_experts_v2.experts.base import LLMTurn
from src.agent.candidate_experts_v2.experts.early_turn_desk import EarlyTurnDeskExpert
from src.agent.candidate_experts_v2.experts.momentum_desk import MomentumDeskExpert
from src.agent.candidate_experts_v2.experts.quality_repair_desk import QualityRepairDeskExpert
from src.agent.candidate_experts_v2.experts.theme_catalyst_desk import ThemeCatalystDeskExpert
from src.agent.candidate_experts_v2.recall import build_recall_pool
from src.agent.candidate_experts_v2.schemas import SeedFactPacket, SeedItem


DEFAULT_TRACE_DIR = (
    ROOT
    / "data/agent_traces/20260530-214336-trace-6ca8c64be6bf431fa27d7ec5e6471dce"
)


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _extract_seed_preview(trace_dir: Path) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for filename in ("seed_pool.json", "candidate_discovery.json", "selection_context.json"):
        path = trace_dir / filename
        if not path.exists():
            continue
        payload = _read_json(path)
        nodes: Iterable[Any] = [payload]
        if filename == "selection_context.json":
            nodes = [
                payload,
                (((payload.get("stages") or {}).get("candidate_discovery") or {}).get("full") or {}),
            ]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            seeds = node.get("seeds")
            if isinstance(seeds, list):
                candidates = [x for x in seeds if isinstance(x, dict)]
                if candidates:
                    return candidates
            summary = node.get("seed_pool_summary")
            if isinstance(summary, dict) and isinstance(summary.get("preview"), list):
                candidates = [x for x in summary["preview"] if isinstance(x, dict)]
                if candidates:
                    return candidates
    raise RuntimeError(f"no seed_pool_summary.preview found under {trace_dir}")


def _seed_items_from_preview(preview: Sequence[Dict[str, Any]]) -> List[SeedItem]:
    seeds: List[SeedItem] = []
    for item in preview:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        source = str(item.get("source") or "fallback")
        try:
            seed = SeedItem(
                code=code,
                name=str(item.get("name") or code),
                market=str(item.get("market") or "cn"),
                source=source,  # type: ignore[arg-type]
                hint=str(item.get("hint") or ""),
                trigger_signals=[
                    sig for sig in (item.get("trigger_signals") or []) if isinstance(sig, dict)
                ],
                priority_score=float(item.get("priority_score") or 0.0),
                freshness=str(item.get("freshness") or "trace"),
                context_hint=str(item.get("context_hint") or item.get("hint") or ""),
                extras={"recall_sources": [source]},
            )
        except Exception:
            seed = SeedItem(
                code=code,
                name=str(item.get("name") or code),
                market="cn",
                source="fallback",
                hint=str(item.get("hint") or ""),
                trigger_signals=[],
                freshness="trace",
                extras={"recall_sources": ["fallback"]},
            )
        seeds.append(seed)
    return seeds


def _load_seed_facts_from_trace(trace_dir: Path) -> Dict[str, SeedFactPacket]:
    path = trace_dir / "seed_facts.json"
    if not path.exists():
        return {}
    payload = _read_json(path)
    packets = payload.get("packets") if isinstance(payload, dict) else payload
    if not isinstance(packets, list):
        return {}
    by_code: Dict[str, SeedFactPacket] = {}
    for item in packets:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        try:
            by_code[code] = SeedFactPacket.model_validate(item)
        except Exception:
            continue
    return by_code


class ProbeLLM:
    """Fast deterministic LLM replacement that records prompt sizes."""

    def __init__(self, *, emit_candidate: bool = True) -> None:
        self.emit_candidate = emit_candidate
        self.calls: List[Dict[str, Any]] = []
        self._current_desk = ""

    def set_context(self, *, desk: str = "") -> None:
        self._current_desk = desk

    def __call__(self, messages: List[Dict[str, Any]], tool_decls: List[Dict[str, Any]]) -> LLMTurn:
        user_text = str(messages[-1].get("content") or "") if messages else ""
        system_text = str(messages[0].get("content") or "") if messages else ""
        code = _extract_first_json_value(user_text, "code") or "UNKNOWN"
        name = _extract_first_json_value(user_text, "name") or code
        self.calls.append(
            {
                "desk": self._current_desk,
                "code": code,
                "seed_code": code,
                "messages": len(messages),
                "tool_decls": len(tool_decls),
                "system_chars": len(system_text),
                "user_chars": len(user_text),
            }
        )
        candidates = []
        if self.emit_candidate and code != "UNKNOWN":
            candidates = [
                {
                    "code": code,
                    "name": name,
                    "market": "cn",
                    "stance": "support",
                    "setup_type": "unknown",
                    "score": 60,
                    "confidence": 0.5,
                    "reason": "probe synthetic output for desk-loop isolation",
                    "evidence": [],
                    "risks": [{"type": "probe", "summary": "synthetic result; not investment advice"}],
                    "valid_until": "next_trading_day",
                }
            ]
        return LLMTurn(
            text=json.dumps(
                {
                    "candidates": candidates,
                    "rejected": [],
                    "data_quality": {"freshness": "probe", "source_chain": ["trace_seed_preview"]},
                },
                ensure_ascii=False,
            )
        )


class RealLLM:
    """Small wrapper around LLMToolAdapter with timing and error surfacing."""

    def __init__(self, timeout_s: float | None) -> None:
        if load_dotenv is not None:
            load_dotenv(ROOT / ".env")
        from src.agent.llm_adapter import LLMToolAdapter

        self.adapter = LLMToolAdapter()
        self.timeout_s = timeout_s
        self.calls: List[Dict[str, Any]] = []
        self._current_desk = ""
        self._round_counts: Dict[str, int] = {}

    def set_context(self, *, desk: str = "") -> None:
        self._current_desk = desk

    def __call__(self, messages: List[Dict[str, Any]], tool_decls: List[Dict[str, Any]]) -> LLMTurn:
        started = time.time()
        response_format = _json_response_format_for_messages(messages)
        resp = self.adapter.call_with_tools(
            messages,
            tool_decls,
            timeout=self.timeout_s,
            response_format=response_format,
        )
        elapsed_ms = int((time.time() - started) * 1000)
        user_text = _last_user_content(messages)
        seed_text = _seed_context_content(messages)
        seed_code = _extract_first_json_value(seed_text, "code") or "UNKNOWN"
        seed_name = _extract_first_json_value(seed_text, "name") or seed_code
        call_key = f"{self._current_desk}:{seed_code}"
        round_idx = self._round_counts.get(call_key, 0)
        self._round_counts[call_key] = round_idx + 1
        provider = str(getattr(resp, "provider", "") or "")
        content = str(getattr(resp, "content", "") or "")
        tool_calls = []
        for tc in getattr(resp, "tool_calls", None) or []:
            tool_calls.append(
                {
                    "name": str(getattr(tc, "name", "") or ""),
                    "arguments": dict(getattr(tc, "arguments", {}) or {}),
                    "call_id": str(getattr(tc, "id", "") or ""),
                }
            )
        content_json = _try_json_loads(content)
        self.calls.append(
            {
                "desk": self._current_desk,
                "seed_code": seed_code,
                "seed_name": seed_name,
                "round": round_idx,
                "elapsed_ms": elapsed_ms,
                "provider": provider,
                "model": str(getattr(resp, "model", "") or ""),
                "tool_decls": len(tool_decls),
                "user_chars": len(user_text),
                "message_count": len(messages),
                "response_format": response_format,
                "tool_calls": tool_calls,
                "content_is_json": content_json is not None,
                "content_json": content_json,
                "content": content,
            }
        )
        if provider == "error" and not tool_calls:
            raise RuntimeError(content or "LLM provider returned error")
        from src.agent.candidate_experts_v2.experts.base import LLMToolCall

        return LLMTurn(
            tool_calls=[
                LLMToolCall(name=tc["name"], arguments=tc["arguments"], call_id=tc["call_id"])
                for tc in tool_calls
            ],
            text=content,
        )


def _extract_first_json_value(text: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]+)"', text)
    return match.group(1) if match else ""


def _last_user_content(messages: Sequence[Dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return str(msg.get("content") or "")
    return str(messages[-1].get("content") or "") if messages else ""


def _seed_context_content(messages: Sequence[Dict[str, Any]]) -> str:
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = str(msg.get("content") or "")
        if '"code"' in content:
            return content
    return _last_user_content(messages)


def _try_json_loads(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _json_response_format_for_messages(messages: Sequence[Dict[str, Any]]) -> Dict[str, str] | None:
    # Keep the first turn free for tool-calling; force JSON only after the
    # model has received tool evidence and is expected to finalize.
    if any(str(msg.get("role") or "") == "tool" for msg in messages or []):
        return {"type": "json_object"}
    return None


def _set_llm_context(llm: Any, *, desk: str) -> None:
    set_context = getattr(llm, "set_context", None)
    if callable(set_context):
        set_context(desk=desk)


def _call_console_summary(call: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "desk": call.get("desk"),
        "seed_code": call.get("seed_code") or call.get("code"),
        "round": call.get("round"),
        "elapsed_ms": call.get("elapsed_ms"),
        "provider": call.get("provider"),
        "model": call.get("model"),
        "content_is_json": call.get("content_is_json"),
        "messages": call.get("messages") or call.get("message_count"),
        "tool_decls": call.get("tool_decls"),
        "user_chars": call.get("user_chars"),
        "system_chars": call.get("system_chars"),
        "tool_call_count": len(call.get("tool_calls") or []),
        "content_chars": len(str(call.get("content") or "")),
    }


def _make_desks(
    llm: Any,
    tool_decls: Sequence[Dict[str, Any]],
    tool_registry: Any,
    *,
    max_llm_rounds: int = 5,
) -> Dict[str, Any]:
    return {
        "early_turn_desk": EarlyTurnDeskExpert(
            tool_registry=tool_registry,
            tool_decls=tool_decls,
            llm=llm,
            max_llm_rounds=max_llm_rounds,
        ),
        "momentum_desk": MomentumDeskExpert(
            tool_registry=tool_registry,
            tool_decls=tool_decls,
            llm=llm,
            max_llm_rounds=max_llm_rounds,
        ),
        "quality_repair_desk": QualityRepairDeskExpert(
            tool_registry=tool_registry,
            tool_decls=tool_decls,
            llm=llm,
            max_llm_rounds=max_llm_rounds,
        ),
        "theme_catalyst_desk": ThemeCatalystDeskExpert(
            tool_registry=tool_registry,
            tool_decls=tool_decls,
            llm=llm,
            max_llm_rounds=max_llm_rounds,
        ),
    }


def _load_real_tool_registry() -> Any:
    from src.agent.factory import get_tool_registry

    registry = get_tool_registry()
    list_names_fn = getattr(registry, "list_names", None)
    execute_fn = getattr(registry, "execute", None)
    if callable(list_names_fn) and callable(execute_fn):
        try:
            return {
                name: (lambda _n: lambda **kw: execute_fn(_n, **kw))(name)
                for name in list_names_fn()
            }
        except Exception:
            return registry
    return registry


def _load_tool_decls(enabled: bool) -> List[Dict[str, Any]]:
    if not enabled:
        return []
    from src.agent.factory import get_tool_registry

    registry = get_tool_registry()
    to_openai_tools = getattr(registry, "to_openai_tools", None)
    return list(to_openai_tools() or []) if callable(to_openai_tools) else []


def _run_probe(
    *,
    label: str,
    llm: Any,
    rows: List[Any],
    tool_decls: Sequence[Dict[str, Any]],
    tool_registry: Any,
    per_seed_timeout_s: float,
    max_llm_rounds: int = 5,
    output_dir: Path | None = None,
) -> None:
    print(f"\n== {label} ==")
    desks = _make_desks(llm, tool_decls, tool_registry, max_llm_rounds=max_llm_rounds)
    packet_payloads: List[Dict[str, Any]] = []
    for name, desk in desks.items():
        _set_llm_context(llm, desk=name)
        started = time.time()
        packet = desk.run_desk(
            rows,
            market="cn",
            regime="trace_probe",
            per_seed_timeout_s=per_seed_timeout_s,
            max_consecutive_seed_timeouts=2,
        )
        elapsed = int((time.time() - started) * 1000)
        per_seed = packet.model_dump(mode="json").get("per_seed_packets") or []
        statuses = [str(item.get("status")) for item in per_seed[:5]]
        errors = "; ".join(str(err) for err in (packet.errors or [])[:3])
        print(
            f"{name}: status={packet.status} seeds={packet.seed_summary.seed_count} "
            f"candidates={len(packet.candidates)} rejected={len(packet.rejected)} "
            f"tools={len(packet.tool_calls)} elapsed_ms={elapsed} first_statuses={statuses}"
        )
        if errors:
            print(f"  errors: {errors}")
        packet_payloads.append(packet.model_dump(mode="json"))
    _set_llm_context(llm, desk="")

    calls = getattr(llm, "calls", [])
    if calls:
        print("llm_calls_preview:")
        for item in calls[:8]:
            if isinstance(item, dict):
                print("  " + json.dumps(_call_console_summary(item), ensure_ascii=False, default=str))
            else:
                print("  " + json.dumps(item, ensure_ascii=False, default=str))

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "label": label,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "packets": packet_payloads,
            "llm_calls": calls,
        }
        json_path = output_dir / f"{label}.json"
        md_path = output_dir / f"{label}.md"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        markdown = _packets_to_markdown(payload)
        md_path.write_text(markdown, encoding="utf-8")
        print(f"wrote_json={json_path}")
        print(f"wrote_md={md_path}")
        if label.startswith("real_llm"):
            reports_path = output_dir / "thesis_desk_final_reports.md"
            reports_path.write_text(markdown, encoding="utf-8")
            print(f"wrote_reports_md={reports_path}")


def _packets_to_markdown(payload: Dict[str, Any]) -> str:
    packets = [packet for packet in (payload.get("packets") or []) if isinstance(packet, dict)]
    calls_by_desk: Dict[str, List[Dict[str, Any]]] = {}
    for call in payload.get("llm_calls") or []:
        if isinstance(call, dict):
            calls_by_desk.setdefault(str(call.get("desk") or "unknown"), []).append(call)

    lines = [
        "# 席位委员会最终输出报告",
        "",
        f"- probe_label: `{payload.get('label')}`",
        f"- generated_at: `{payload.get('generated_at')}`",
        "",
        "## 总览",
        "",
        "| 席位 | status | candidates | rejected | tools | final_json | errors |",
        "| --- | --- | ---: | ---: | ---: | --- | --- |",
    ]

    for packet in packets:
        expert = str(packet.get("expert") or "")
        final_call = _final_json_call(calls_by_desk.get(expert) or [])
        errors = "；".join(str(err) for err in (packet.get("errors") or [])) or "-"
        lines.append(
            "| {expert} | `{status}` | {candidates} | {rejected} | {tools} | {final_json} | {errors} |".format(
                expert=expert,
                status=packet.get("status"),
                candidates=len(packet.get("candidates") or []),
                rejected=len(packet.get("rejected") or []),
                tools=len(packet.get("tool_calls") or []),
                final_json="yes" if final_call is not None else "no",
                errors=errors.replace("|", "\\|"),
            )
        )
    lines.append("")

    for packet in packets:
        expert = str(packet.get("expert") or "")
        calls = calls_by_desk.get(expert) or []
        lines.extend(
            [
                f"## {expert}",
                "",
                "### 运行摘要",
                "",
                f"- status: `{packet.get('status')}`",
                f"- seed_count: `{(packet.get('seed_summary') or {}).get('seed_count')}`",
                f"- candidate_count: `{len(packet.get('candidates') or [])}`",
                f"- rejected_count: `{len(packet.get('rejected') or [])}`",
                f"- tool_call_count: `{len(packet.get('tool_calls') or [])}`",
                f"- elapsed_ms: `{packet.get('elapsed_ms')}`",
                "",
            ]
        )
        if packet.get("errors"):
            lines.extend(["### 错误", ""])
            for err in packet.get("errors") or []:
                lines.append(f"- {err}")
            lines.append("")

        lines.extend(_llm_calls_markdown(calls))

        final_json = _final_json_payload(packet, calls)
        lines.extend(
            [
                "### 最终席位 JSON",
                "",
                "```json",
                json.dumps(final_json, ensure_ascii=False, indent=2, default=str),
                "```",
                "",
            ]
        )

        lines.extend(["### 解析结果", ""])
        if packet.get("candidates"):
            lines.extend(["#### 入选 candidates", ""])
            for cand in packet.get("candidates") or []:
                lines.extend(_json_block(cand))
        else:
            lines.extend(["#### 入选 candidates", "", "```json", "[]", "```", ""])

        if packet.get("rejected"):
            lines.extend(["#### 剔除 rejected", ""])
            for item in packet.get("rejected") or []:
                lines.extend(_json_block(item))
        else:
            lines.extend(["#### 剔除 rejected", "", "```json", "[]", "```", ""])

        if packet.get("tool_calls"):
            lines.extend(["### 工具执行结果", ""])
            lines.extend(_json_block(packet.get("tool_calls")))

        per_seed = packet.get("per_seed_packets") or []
        if per_seed:
            lines.extend(["### 调试包 per_seed_packets", ""])
            for item in per_seed:
                lines.extend(_json_block(item))
    return "\n".join(lines)


def _json_block(value: Any) -> List[str]:
    return ["```json", json.dumps(value, ensure_ascii=False, indent=2, default=str), "```", ""]


def _final_json_call(calls: Sequence[Dict[str, Any]]) -> Dict[str, Any] | None:
    for call in reversed(list(calls)):
        if call.get("content_is_json") and isinstance(call.get("content_json"), dict):
            return call
    return None


def _final_json_payload(packet: Dict[str, Any], calls: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    final_call = _final_json_call(calls)
    content = final_call.get("content_json") if final_call else None
    if isinstance(content, dict) and set(content.keys()) >= {"data_quality", "candidates", "rejected"}:
        return content
    return {
        "data_quality": packet.get("data_quality") or {},
        "candidates": packet.get("candidates") or [],
        "rejected": packet.get("rejected") or [],
    }


def _llm_calls_markdown(calls: Sequence[Dict[str, Any]]) -> List[str]:
    lines: List[str] = ["### LLM 调用过程", ""]
    if not calls:
        lines.extend(["_没有记录到 LLM calls。_", ""])
        return lines

    calls_by_seed: Dict[str, List[Dict[str, Any]]] = {}
    for call in calls:
        calls_by_seed.setdefault(str(call.get("seed_code") or "UNKNOWN"), []).append(call)
    for seed_code, seed_calls in calls_by_seed.items():
        seed_name = str(seed_calls[0].get("seed_name") or seed_code)
        lines.extend([f"#### {seed_code} {seed_name}", ""])
        for call in sorted(seed_calls, key=lambda item: int(item.get("round") or 0)):
            round_idx = call.get("round")
            lines.extend(
                [
                    f"##### round {round_idx}",
                    "",
                    f"- elapsed_ms: `{call.get('elapsed_ms')}`",
                    f"- provider/model: `{call.get('provider')}` / `{call.get('model')}`",
                    f"- response_format: `{json.dumps(call.get('response_format'), ensure_ascii=False)}`",
                    f"- content_is_json: `{call.get('content_is_json')}`",
                    f"- requested_tool_calls: `{len(call.get('tool_calls') or [])}`",
                    "",
                ]
            )
            if call.get("tool_calls"):
                lines.extend(["工具请求:", ""])
                lines.extend(_json_block(call.get("tool_calls")))
            elif call.get("content_is_json"):
                lines.extend(["模型最终 JSON:", ""])
                lines.extend(_json_block(call.get("content_json")))
            elif call.get("content"):
                lines.extend(
                    [
                        "模型原始文本（非 JSON）:",
                        "",
                        "```text",
                        str(call.get("content") or ""),
                        "```",
                        "",
                    ]
                )
    return lines


def _run_direct_llm_probe(
    *,
    rows: List[Any],
    tool_decls: Sequence[Dict[str, Any]],
    llm_timeout_s: float | None,
) -> None:
    print("\n== direct_real_llm_first_turn_no_outer_guard ==")
    real = RealLLM(timeout_s=llm_timeout_s)
    desks = _make_desks(real, tool_decls, {}, max_llm_rounds=1)
    from src.agent.candidate_experts_v2.experts.desk_base import _seed_from_row

    for name, desk in desks.items():
        eligible = desk._filter_eligible_rows(rows)
        if not eligible:
            print(f"{name}: no eligible rows")
            continue
        row = eligible[0]
        desk._desk_rows = [row]
        desk._desk_regime = "trace_probe"
        user_message = desk._build_user_message([_seed_from_row(row)], market="cn")
        desk._desk_rows = []
        desk._desk_regime = "unknown"
        messages = [
            {"role": "system", "content": desk.system_prompt},
            {"role": "user", "content": user_message},
        ]
        started = time.time()
        try:
            real.set_context(desk=name)
            turn = real(messages, list(desk.tool_decls))
            elapsed_ms = int((time.time() - started) * 1000)
            print(
                f"{name}: code={row.code} elapsed_ms={elapsed_ms} "
                f"tool_calls={len(turn.tool_calls)} text_chars={len(turn.text or '')}"
            )
            if turn.text:
                print(f"  text_preview: {str(turn.text)[:240]}")
            if turn.tool_calls:
                print(
                    "  tool_calls_preview: "
                    + json.dumps(
                        [
                            {"name": call.name, "arguments": call.arguments}
                            for call in turn.tool_calls[:3]
                        ],
                        ensure_ascii=False,
                    )
                )
        except Exception as exc:
            elapsed_ms = int((time.time() - started) * 1000)
            print(f"{name}: code={row.code} elapsed_ms={elapsed_ms} error={type(exc).__name__}: {exc}")

    if real.calls:
        print("direct_llm_calls:")
        for item in real.calls:
            print("  " + json.dumps(_call_console_summary(item), ensure_ascii=False, default=str))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-dir", type=Path, default=DEFAULT_TRACE_DIR)
    parser.add_argument("--real-llm", action="store_true", help="also call the configured real LLM")
    parser.add_argument("--direct-llm-only", action="store_true", help="call the real LLM once per desk without the desk per-seed guard")
    parser.add_argument("--with-tool-schemas", action="store_true", help="send real OpenAI tool schemas")
    parser.add_argument("--with-real-tools", action="store_true", help="execute real ToolRegistry tools when the model requests them")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--max-llm-rounds", type=int, default=5)
    parser.add_argument("--llm-timeout-s", type=float, default=8.0, help="<=0 means no adapter timeout")
    parser.add_argument("--per-seed-timeout-s", type=float, default=12.0)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    preview = _extract_seed_preview(args.trace_dir)[: args.limit]
    seeds = _seed_items_from_preview(preview)
    pool = SeedPoolBuildResult(seeds=seeds, total_limit=max(len(seeds), args.limit))
    recall = build_recall_pool(
        market="cn",
        seed_symbols=[],
        tool_registry={},
        coarse_cap=max(120, len(seeds)),
        prebuilt_pool=pool,
    )
    rows = recall.rows
    seed_facts = _load_seed_facts_from_trace(args.trace_dir)
    attached_seed_facts = 0
    for row in rows:
        packet = seed_facts.get(row.code)
        if packet is not None:
            row.seed_fact = packet
            attached_seed_facts += 1
    print(
        f"trace={args.trace_dir}\n"
        f"seed_preview={len(preview)} seed_items={len(seeds)} "
        f"recall_rows={len(rows)} attached_seed_facts={attached_seed_facts} "
        f"recall_sources={json.dumps(recall.sources, ensure_ascii=False)}"
    )
    print("first_rows=" + json.dumps([{"code": r.code, "name": r.name, "sources": r.recall_sources} for r in rows[:5]], ensure_ascii=False))

    tool_decls = _load_tool_decls(args.with_tool_schemas)
    tool_registry = _load_real_tool_registry() if args.with_real_tools else {}
    llm_timeout_s = None if args.llm_timeout_s <= 0 else float(args.llm_timeout_s)
    if args.direct_llm_only:
        _run_direct_llm_probe(
            rows=rows,
            tool_decls=tool_decls,
            llm_timeout_s=llm_timeout_s,
        )
        return
    fake = ProbeLLM()
    _run_probe(
        label="fake_llm_no_network",
        llm=fake,
        rows=rows,
        tool_decls=tool_decls,
        tool_registry={},
        per_seed_timeout_s=args.per_seed_timeout_s,
        max_llm_rounds=args.max_llm_rounds,
        output_dir=args.output_dir,
    )

    if args.real_llm:
        real = RealLLM(timeout_s=llm_timeout_s)
        _run_probe(
            label=f"real_llm_timeout_{llm_timeout_s if llm_timeout_s is not None else 'none'}s",
            llm=real,
            rows=rows,
            tool_decls=tool_decls,
            tool_registry=tool_registry,
            per_seed_timeout_s=args.per_seed_timeout_s,
            max_llm_rounds=args.max_llm_rounds,
            output_dir=args.output_dir,
        )


if __name__ == "__main__":
    main()
