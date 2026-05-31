# -*- coding: utf-8 -*-
"""Pre-desk SeedFactPacket builder.

This layer fetches common per-stock facts once before the three thesis desks
run.  It is intentionally independent from desk tool budgets: failures are
recorded as data-quality metadata and passed downstream instead of being hidden.
"""

from __future__ import annotations

import concurrent.futures
import math
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.agent.candidate_experts_v2.schemas import (
    FactSheet,
    FeatureRow,
    SeedFactDataQuality,
    SeedFactPacket,
    SeedFactToolResult,
)


DEFAULT_SEED_FACT_TOOLS: Tuple[str, ...] = (
    "analyze_price_structure",
    "analyze_trend",
    "calculate_ma",
    "get_volume_analysis",
    "get_capital_flow",
    "get_stock_info",
)


def build_seed_fact_packets_parallel(
    rows: Sequence[FeatureRow],
    *,
    tool_registry: Any,
    tools: Optional[Sequence[str]] = None,
    max_workers: int = 12,
    tool_timeout_seconds: float = 12.0,
) -> List[SeedFactPacket]:
    """Build one shared SeedFactPacket per row using (seed, tool) tasks."""

    started = time.time()
    selected_tools = _normalize_tools(tools)
    packets: Dict[str, SeedFactPacket] = {
        row.code: _empty_packet(row, selected_tools) for row in rows
    }
    if not rows or not selected_tools:
        return list(packets.values())

    task_specs: List[Tuple[str, str, Callable[..., Any]]] = []
    for row in rows:
        for tool_name in selected_tools:
            fn = _lookup_tool(tool_registry, tool_name)
            if fn is None:
                result = packets[row.code].facts[tool_name]
                result.status = "missing"
                result.errors = [f"tool {tool_name} not registered"]
                packets[row.code].tool_calls.append(
                    {
                        "stock_code": row.code,
                        "tool": tool_name,
                        "status": "missing",
                        "error": result.errors[0],
                        "elapsed_ms": 0,
                    }
                )
                continue
            task_specs.append((row.code, tool_name, fn))

    if task_specs:
        worker_count = max(1, min(int(max_workers or 1), len(task_specs)))
        overall_timeout = max(
            float(tool_timeout_seconds),
            float(tool_timeout_seconds) * math.ceil(len(task_specs) / worker_count) + 1.0,
        )
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
        futures = {
            pool.submit(_call_tool, fn, code, tool_name): (code, tool_name)
            for code, tool_name, fn in task_specs
        }
        completed = set()
        try:
            for future in concurrent.futures.as_completed(futures, timeout=overall_timeout):
                code, tool_name = futures[future]
                completed.add(future)
                try:
                    result, call_log = future.result()
                except Exception as exc:  # pragma: no cover - defensive
                    result = SeedFactToolResult(
                        status="failed",
                        errors=[str(exc)],
                    )
                    call_log = {
                        "stock_code": code,
                        "tool": tool_name,
                        "status": "failed",
                        "error": str(exc),
                        "elapsed_ms": 0,
                    }
                packets[code].facts[tool_name] = result
                packets[code].tool_calls.append(call_log)
        except concurrent.futures.TimeoutError:
            pass
        finally:
            for future, (code, tool_name) in futures.items():
                if future in completed:
                    continue
                future.cancel()
                result = packets[code].facts[tool_name]
                result.status = "timeout"
                result.errors = [f"tool {tool_name} timed out after {tool_timeout_seconds:.1f}s"]
                packets[code].tool_calls.append(
                    {
                        "stock_code": code,
                        "tool": tool_name,
                        "status": "timeout",
                        "error": result.errors[0],
                        "elapsed_ms": int(float(tool_timeout_seconds) * 1000),
                    }
                )
            pool.shutdown(wait=False, cancel_futures=True)

    total_elapsed_ms = int((time.time() - started) * 1000)
    for packet in packets.values():
        _finalize_packet_quality(packet, elapsed_ms=total_elapsed_ms)
    return list(packets.values())


def summarize_seed_fact_packets(packets: Sequence[SeedFactPacket]) -> Dict[str, Any]:
    """Return compact trace payload for the pre-desk facts layer."""

    status_counts: Dict[str, int] = {"ok": 0, "partial": 0, "failed": 0}
    tool_status_counts: Dict[str, Dict[str, int]] = {}
    elapsed_ms = 0
    preview: List[Dict[str, Any]] = []
    for packet in packets:
        status = str(packet.data_quality.status or "failed")
        status_counts[status] = status_counts.get(status, 0) + 1
        elapsed_ms = max(elapsed_ms, int(packet.data_quality.elapsed_ms or 0))
        for tool_name, result in packet.facts.items():
            tool_counts = tool_status_counts.setdefault(tool_name, {})
            tool_counts[result.status] = tool_counts.get(result.status, 0) + 1
        if len(preview) < 10:
            preview.append(
                {
                    "code": packet.code,
                    "name": packet.name,
                    "status": packet.data_quality.status,
                    "ok_tools": packet.data_quality.ok_tools,
                    "failed_tools": packet.data_quality.failed_tools,
                    "missing_tools": packet.data_quality.missing_tools,
                }
            )
    return {
        "total": len(packets),
        "ok": status_counts.get("ok", 0),
        "partial": status_counts.get("partial", 0),
        "failed": status_counts.get("failed", 0),
        "elapsed_ms": elapsed_ms,
        "packets_ref": "seed_facts.json",
        "tool_status_counts": tool_status_counts,
        "packets_preview": preview,
    }


def compact_seed_fact_packets_for_model(
    packets: Sequence[Any],
    *,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Compress SeedFactPackets into the payload intended for model context."""

    compact: List[Dict[str, Any]] = []
    for packet in list(packets or [])[:limit]:
        packet_dict = _packet_to_dict(packet)
        if not packet_dict:
            continue
        facts = packet_dict.get("facts") if isinstance(packet_dict.get("facts"), dict) else {}
        compact.append(
            {
                "code": packet_dict.get("code"),
                "name": packet_dict.get("name"),
                "market": packet_dict.get("market"),
                "recall_sources": packet_dict.get("recall_sources") or [],
                "flags": _compact_flags(packet_dict.get("flags")),
                "fact_sheet": _compact_fact_sheet(packet_dict.get("fact_sheet")),
                "data_quality": _compact_data_quality(packet_dict.get("data_quality")),
                "facts": {
                    str(tool_name): _compact_tool_fact(str(tool_name), result)
                    for tool_name, result in facts.items()
                    if isinstance(result, dict)
                },
            }
        )
    return compact


def _packet_to_dict(packet: Any) -> Dict[str, Any]:
    if isinstance(packet, SeedFactPacket):
        return packet.model_dump(mode="json")
    if isinstance(packet, dict):
        return packet
    return {}


def _compact_flags(value: Any) -> List[Dict[str, Any]]:
    flags = value if isinstance(value, list) else []
    compact: List[Dict[str, Any]] = []
    for flag in flags[:6]:
        if not isinstance(flag, dict):
            continue
        compact.append(
            {
                "detector": flag.get("detector"),
                "kind": flag.get("kind"),
                "summary": flag.get("summary"),
            }
        )
    return compact


def _compact_fact_sheet(value: Any) -> Dict[str, Any]:
    fs = value if isinstance(value, dict) else {}
    keep = (
        "capital_direction",
        "capital_violent_outflow",
        "trend_state",
        "breakdown_accelerating",
        "range_pct_60",
        "range_pct_120",
        "dist_to_high_20",
        "gain_5d",
        "bias_ma20",
        "volume_ratio",
        "rsi14",
        "liquidity_ok",
        "sector_name",
        "sector_strength",
        "freshness",
        "warnings",
    )
    return {
        key: fs.get(key)
        for key in keep
        if key in fs and fs.get(key) not in (None, "", [], {})
    }


def _compact_data_quality(value: Any) -> Dict[str, Any]:
    dq = value if isinstance(value, dict) else {}
    keep = (
        "status",
        "tool_count",
        "ok_tools",
        "partial_tools",
        "failed_tools",
        "missing_tools",
        "elapsed_ms",
    )
    return {key: dq.get(key) for key in keep if key in dq}


def _compact_tool_fact(tool_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    existing_summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    compact = {
        "status": result.get("status"),
        "summary": _slim_tool_summary(
            tool_name,
            existing_summary or _extract_tool_summary(tool_name, data),
        ),
    }
    errors = _compact_errors(result.get("errors"))
    if errors:
        compact["errors"] = errors
    return compact


def _slim_tool_summary(tool_name: str, summary: Dict[str, Any]) -> Dict[str, Any]:
    if tool_name == "analyze_price_structure":
        latest = summary.get("latest_bar") if isinstance(summary.get("latest_bar"), dict) else {}
        chan = summary.get("chan") if isinstance(summary.get("chan"), dict) else {}
        smc = summary.get("smc") if isinstance(summary.get("smc"), dict) else {}
        return {
            key: value
            for key, value in {
                "status": summary.get("status"),
                "data_quality": summary.get("data_quality"),
                "bar_count": summary.get("bar_count"),
                "latest_bar": {
                    "time": latest.get("time"),
                    "close": latest.get("close"),
                    "high": latest.get("high"),
                    "low": latest.get("low"),
                    "volume": latest.get("volume"),
                } if latest else None,
                "chan": _compact_price_structure_chan(chan),
                "smc": _compact_price_structure_smc(smc),
                "source": summary.get("source"),
                "period_days": summary.get("period_days"),
            }.items()
            if value not in (None, "", [], {})
        }
    if tool_name == "analyze_trend":
        keep = (
            "trend_status",
            "ma_alignment",
            "trend_strength",
            "current_price",
            "ma5",
            "ma10",
            "ma20",
            "ma60",
            "bias_ma5",
            "bias_ma10",
            "bias_ma20",
            "volume_status",
            "volume_ratio_5d",
            "volume_trend",
            "support_ma5",
            "support_ma10",
            "support_levels",
            "resistance_levels",
            "macd_status",
            "macd_signal",
            "rsi_6",
            "rsi_12",
            "rsi_24",
            "rsi_status",
            "rsi_signal",
            "buy_signal",
            "signal_score",
            "signal_reasons",
            "risk_factors",
        )
        result = {key: summary.get(key) for key in keep if summary.get(key) is not None}
        if isinstance(result.get("support_levels"), list):
            result["support_levels"] = result["support_levels"][:5]
        if isinstance(result.get("resistance_levels"), list):
            result["resistance_levels"] = result["resistance_levels"][:5]
        if isinstance(result.get("signal_reasons"), list):
            result["signal_reasons"] = [str(item)[:160] for item in result["signal_reasons"][:5]]
        if isinstance(result.get("risk_factors"), list):
            result["risk_factors"] = [str(item)[:160] for item in result["risk_factors"][:5]]
        return result
    if tool_name == "calculate_ma":
        ma = summary.get("ma") if isinstance(summary.get("ma"), dict) else {}
        return {
            key: value
            for key, value in {
                "current_price": summary.get("current_price"),
                "data_points": summary.get("data_points"),
                "above_ma_count": summary.get("above_ma_count"),
                "total_ma_count": summary.get("total_ma_count"),
                "ma_alignment": summary.get("ma_alignment"),
                "ma": {
                    str(name): {
                        "value": item.get("value"),
                        "bias_pct": item.get("bias_pct"),
                        "price_above": item.get("price_above"),
                    }
                    for name, item in ma.items()
                    if name in {"ma5", "ma10", "ma20", "ma60", "ma120"}
                    and isinstance(item, dict)
                },
            }.items()
            if value not in (None, "", [], {})
        }
    if tool_name == "get_volume_analysis":
        keep = (
            "period_days",
            "latest_volume",
            "avg_volume_5d",
            "avg_volume_20d",
            "volume_ratio_vs_5d",
            "volume_ratio_vs_20d",
            "volume_trend",
            "volume_trend_pct",
            "price_volume_relation",
        )
        return {key: summary.get(key) for key in keep if summary.get(key) is not None}
    if tool_name == "get_capital_flow":
        sector_rankings = summary.get("sector_rankings") if isinstance(summary.get("sector_rankings"), dict) else {}
        keep = (
            "status",
            "main_net_inflow",
            "inflow_5d",
            "inflow_10d",
            "latest_date",
            "error_summary",
        )
        result = {key: summary.get(key) for key in keep if summary.get(key) not in (None, "", [], {})}
        compact_sectors = _compact_sector_rankings(sector_rankings)
        if compact_sectors:
            result["sector_rankings"] = compact_sectors
        return result
    if tool_name == "get_stock_info":
        boards = summary.get("belong_boards") if isinstance(summary.get("belong_boards"), list) else []
        fundamental = summary.get("fundamental_context")
        coverage: Any = None
        if isinstance(fundamental, dict):
            coverage = fundamental.get("coverage")
        fundamental_summary = _compact_fundamental_context_for_model(fundamental)
        return {
            key: value
            for key, value in {
                "status": summary.get("status"),
                "code": summary.get("code"),
                "name": summary.get("name"),
                "pe_ratio": summary.get("pe_ratio"),
                "pb_ratio": summary.get("pb_ratio"),
                "total_mv": summary.get("total_mv"),
                "circ_mv": summary.get("circ_mv"),
                "belong_boards": [
                    {
                        "name": item.get("name"),
                        "code": item.get("code"),
                    }
                    for item in boards[:5]
                    if isinstance(item, dict)
                ],
                "coverage": coverage,
                "fundamental_context": fundamental_summary,
                "sector_rankings": _compact_sector_rankings(summary.get("sector_rankings")),
            }.items()
            if value not in (None, "", [], {})
        }
    return {
        key: value
        for key, value in summary.items()
        if value not in (None, "", [], {})
    }


def _compact_price_structure_chan(chan: Dict[str, Any]) -> Dict[str, Any]:
    if not chan:
        return {}
    summary = chan.get("structure_summary") if isinstance(chan.get("structure_summary"), dict) else {}
    latest_centers = chan.get("latest_centers") if isinstance(chan.get("latest_centers"), list) else []
    latest_pens = chan.get("latest_pens") if isinstance(chan.get("latest_pens"), list) else []
    unfinished = chan.get("unfinished_pen") if isinstance(chan.get("unfinished_pen"), dict) else {}
    return {
        key: value
        for key, value in {
            "status": chan.get("status"),
            "pen_count": chan.get("pen_count"),
            "center_count": chan.get("center_count"),
            "structure_summary": _compact_value(summary),
            "latest_center": _compact_value(latest_centers[-1]) if latest_centers else None,
            "latest_pens": [_compact_value(item) for item in latest_pens[-3:]],
            "unfinished_pen": _compact_value(unfinished),
        }.items()
        if value not in (None, "", [], {})
    }


def _compact_price_structure_smc(smc: Dict[str, Any]) -> Dict[str, Any]:
    if not smc:
        return {}
    summary = smc.get("structure_summary") if isinstance(smc.get("structure_summary"), dict) else {}
    swings = smc.get("latest_swings") if isinstance(smc.get("latest_swings"), list) else []
    return {
        key: value
        for key, value in {
            "status": smc.get("status"),
            "swing_count": smc.get("swing_count"),
            "structure_summary": _compact_value(summary),
            "bos": _compact_value(smc.get("bos")),
            "choch": _compact_value(smc.get("choch")),
            "latest_swings": [_compact_value(item) for item in swings[-5:]],
            "order_blocks": _compact_value((smc.get("order_blocks") or [])[-3:]) if isinstance(smc.get("order_blocks"), list) else None,
            "fair_value_gaps": _compact_value((smc.get("fair_value_gaps") or [])[-3:]) if isinstance(smc.get("fair_value_gaps"), list) else None,
        }.items()
        if value not in (None, "", [], {})
    }


def _compact_sector_rankings(value: Any) -> Dict[str, Any]:
    rankings = value if isinstance(value, dict) else {}
    compact: Dict[str, Any] = {}
    for key in ("top_inflow_sectors", "top_outflow_sectors", "top", "bottom"):
        items = rankings.get(key)
        if not isinstance(items, list):
            continue
        compact[key] = [
            _compact_value(item)
            for item in items[:3]
            if isinstance(item, dict)
        ]
    return {key: value for key, value in compact.items() if value}


def _compact_fundamental_context_for_model(value: Any) -> Dict[str, Any]:
    context = value if isinstance(value, dict) else {}
    if not context:
        return {}
    compact: Dict[str, Any] = {
        key: context.get(key)
        for key in ("market", "status", "coverage")
        if context.get(key) not in (None, "", [], {})
    }
    for block in (
        "valuation",
        "growth",
        "earnings",
        "institution",
        "capital_flow",
        "dragon_tiger",
        "boards",
    ):
        payload = context.get(block)
        if not isinstance(payload, dict):
            continue
        block_payload: Dict[str, Any] = {}
        if payload.get("status") not in (None, "", [], {}):
            block_payload["status"] = payload.get("status")
        data = payload.get("data")
        if isinstance(data, dict):
            block_payload["data"] = _compact_value(data)
        if block_payload:
            compact[block] = block_payload
    return compact


def _compact_errors(value: Any) -> List[str]:
    errors = value if isinstance(value, list) else ([] if not value else [value])
    return [str(item)[:240] for item in errors[:3] if str(item).strip()]


def _extract_tool_summary(tool_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
    fields_by_tool: Dict[str, Tuple[str, ...]] = {
        "analyze_price_structure": (
            "status",
            "data_quality",
            "bar_count",
            "latest_bar",
            "chan",
            "smc",
            "notes",
            "source",
            "period_days",
        ),
        "analyze_trend": (
            "trend_status",
            "ma_alignment",
            "trend_strength",
            "current_price",
            "bias_ma5",
            "bias_ma10",
            "ma5",
            "ma10",
            "ma20",
            "ma60",
            "bias_ma20",
            "volume_status",
            "volume_ratio_5d",
            "volume_trend",
            "support_ma5",
            "support_ma10",
            "support_levels",
            "resistance_levels",
            "macd_status",
            "macd_signal",
            "rsi_6",
            "rsi_12",
            "rsi_24",
            "rsi_status",
            "rsi_signal",
            "buy_signal",
            "signal_score",
            "signal_reasons",
            "risk_factors",
        ),
        "calculate_ma": (
            "current_price",
            "data_points",
            "above_ma_count",
            "total_ma_count",
            "ma_alignment",
            "ma",
        ),
        "get_volume_analysis": (
            "period_days",
            "latest_volume",
            "avg_volume_5d",
            "avg_volume_20d",
            "volume_ratio_vs_5d",
            "volume_ratio_vs_20d",
            "volume_trend",
            "volume_trend_pct",
            "price_volume_relation",
        ),
        "get_capital_flow": (
            "status",
            "main_net_inflow",
            "inflow_5d",
            "inflow_10d",
            "latest_date",
            "source_update",
            "source_chain",
            "error_summary",
        ),
        "get_stock_info": (
            "status",
            "code",
            "name",
            "pe_ratio",
            "pb_ratio",
            "total_mv",
            "circ_mv",
            "belong_boards",
            "belong_boards_errors",
            "fundamental_context",
        ),
    }
    keep = fields_by_tool.get(tool_name, ("status", "source", "data_quality", "summary", "message"))
    if tool_name in fields_by_tool:
        return {
            key: data.get(key)
            for key in keep
            if key in data and data.get(key) is not None
        }
    return {
        key: _compact_value(data.get(key))
        for key in keep
        if key in data and data.get(key) is not None
    }


def _compact_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 2:
        if isinstance(value, dict):
            return {"keys": list(value.keys())[:8]}
        if isinstance(value, list):
            return f"{len(value)} items"
        return value
    if isinstance(value, dict):
        compact: Dict[str, Any] = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= 12:
                compact["_truncated_keys"] = len(value) - idx
                break
            compact[str(key)] = _compact_value(item, depth=depth + 1)
        return compact
    if isinstance(value, list):
        return [_compact_value(item, depth=depth + 1) for item in value[:5]]
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "...[truncated]"
    return value


def _normalize_tools(tools: Optional[Sequence[str]]) -> List[str]:
    raw = list(tools or DEFAULT_SEED_FACT_TOOLS)
    normalized: List[str] = []
    for item in raw:
        name = str(item or "").strip()
        if name and name not in normalized:
            normalized.append(name)
    return normalized


def _empty_packet(row: FeatureRow, tools: Sequence[str]) -> SeedFactPacket:
    return SeedFactPacket(
        code=row.code,
        name=row.name,
        market=row.market,
        recall_sources=list(row.recall_sources or []),
        flags=[
            {
                "detector": flag.detector,
                "kind": flag.kind,
                "summary": flag.summary,
                "metrics": flag.metrics,
                "as_of": flag.as_of,
            }
            for flag in row.flags
        ],
        fact_sheet=_fact_sheet_to_dict(row.fact_sheet),
        facts={tool_name: SeedFactToolResult(status="missing") for tool_name in tools},
    )


def _fact_sheet_to_dict(fact_sheet: Optional[FactSheet]) -> Dict[str, Any]:
    if fact_sheet is None:
        return {}
    try:
        return fact_sheet.model_dump(mode="json")
    except Exception:
        return {}


def _lookup_tool(tool_registry: Any, name: str) -> Optional[Callable[..., Any]]:
    if isinstance(tool_registry, Mapping):
        fn = tool_registry.get(name)
        return fn if callable(fn) else None
    get_fn = getattr(tool_registry, "get", None)
    if callable(get_fn):
        try:
            fn = get_fn(name)
            if callable(fn):
                return fn
        except Exception:
            pass
    execute_fn = getattr(tool_registry, "execute", None)
    if callable(execute_fn):
        return lambda **kwargs: execute_fn(name, **kwargs)
    return None


def _call_tool(fn: Callable[..., Any], code: str, tool_name: str) -> Tuple[SeedFactToolResult, Dict[str, Any]]:
    started = time.time()
    try:
        raw = fn(stock_code=code)
        elapsed_ms = int((time.time() - started) * 1000)
        status = _classify_tool_status(raw)
        errors = _extract_errors(raw)
        result = SeedFactToolResult(
            status=status,
            data=_sanitize_json_payload(raw),
            errors=errors,
            elapsed_ms=elapsed_ms,
        )
        call_log = {
            "stock_code": code,
            "tool": tool_name,
            "status": status,
            "elapsed_ms": elapsed_ms,
        }
        if errors:
            call_log["error"] = "; ".join(errors[:3])
        return result, call_log
    except Exception as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        return (
            SeedFactToolResult(
                status="failed",
                data={},
                errors=[str(exc)],
                elapsed_ms=elapsed_ms,
            ),
            {
                "stock_code": code,
                "tool": tool_name,
                "status": "failed",
                "error": str(exc),
                "elapsed_ms": elapsed_ms,
            },
        )


def _classify_tool_status(raw: Any) -> str:
    if not isinstance(raw, dict):
        return "ok"
    status = str(raw.get("status") or raw.get("data_quality") or "").strip().lower()
    if status in {"failed", "error", "tool_failed", "not_supported"}:
        return "failed"
    if status in {"timeout", "timed_out"} or raw.get("timeout") is True:
        return "timeout"
    if status in {"partial", "limited", "insufficient", "insufficient_data"}:
        return "partial"
    if raw.get("success") is False or raw.get("error"):
        return "failed"
    return "ok"


def _extract_errors(raw: Any) -> List[str]:
    if not isinstance(raw, dict):
        return []
    values: List[str] = []
    for key in ("error", "message", "reason"):
        value = raw.get(key)
        if value and _classify_tool_status(raw) in {"failed", "timeout"}:
            values.append(str(value))
    errors = raw.get("errors")
    if isinstance(errors, list):
        values.extend(str(item) for item in errors if str(item).strip())
    elif errors:
        values.append(str(errors))
    return list(dict.fromkeys(values))


def _finalize_packet_quality(packet: SeedFactPacket, *, elapsed_ms: int) -> None:
    ok_tools: List[str] = []
    partial_tools: List[str] = []
    failed_tools: List[str] = []
    missing_tools: List[str] = []
    for tool_name, result in packet.facts.items():
        if result.status == "ok":
            ok_tools.append(tool_name)
        elif result.status == "partial":
            partial_tools.append(tool_name)
        elif result.status == "missing":
            missing_tools.append(tool_name)
        else:
            failed_tools.append(tool_name)

    if ok_tools and not failed_tools and not missing_tools and not partial_tools:
        status = "ok"
    elif ok_tools or partial_tools:
        status = "partial"
    else:
        status = "failed"
    packet.data_quality = SeedFactDataQuality(
        status=status,  # type: ignore[arg-type]
        tool_count=len(packet.facts),
        ok_tools=len(ok_tools),
        partial_tools=partial_tools,
        failed_tools=failed_tools,
        missing_tools=missing_tools,
        elapsed_ms=elapsed_ms,
    )


def _sanitize_json_payload(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _sanitize_json_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_json_payload(item) for item in value]
    return value


__all__ = [
    "DEFAULT_SEED_FACT_TOOLS",
    "build_seed_fact_packets_parallel",
    "compact_seed_fact_packets_for_model",
    "summarize_seed_fact_packets",
]
