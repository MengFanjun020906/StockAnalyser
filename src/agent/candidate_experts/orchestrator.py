# -*- coding: utf-8 -*-
"""First-stage multi-expert candidate discovery orchestration."""

from __future__ import annotations

import concurrent.futures
import math
import os
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from src.agent.candidate_experts.filters import apply_hard_exclusion
from src.agent.candidate_experts.schemas import (
    CandidateDataQuality,
    ExpertCandidate,
    ExpertCandidatePacket,
    ThemeWatchItem,
)
from src.agent.candidate_providers.alphasift_provider import AlphaSiftCandidateProvider
from src.agent.candidate_providers.fundamental_provider import FundamentalCandidateProvider
from src.agent.candidate_providers.sequoia_provider import SequoiaCandidateProvider


CandidateToolFn = Callable[..., Any]


def _today_valid_until(days: int = 1) -> str:
    return (datetime.now() + timedelta(days=days)).date().isoformat()


def _score_confidence(score: Any, default: float = 0.55) -> float:
    try:
        value = float(score)
    except Exception:
        return default
    return max(0.2, min(0.9, value / 100.0))


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _candidate_from_raw(
    item: Dict[str, Any],
    *,
    expert: str,
    dimension: str,
    default_reason: str,
    evidence_prefix: str,
    default_confidence: float = 0.55,
) -> ExpertCandidate:
    code = str(item.get("code") or item.get("stock_code") or "").strip()
    name = str(item.get("name") or item.get("stock_name") or code).strip()
    score = float(item.get("signal_score") or item.get("score") or 50.0)
    source = str(item.get("source") or evidence_prefix).strip()
    reason = str(item.get("reason") or default_reason).strip()
    evidence_refs = list(dict.fromkeys([source, *[str(src) for src in _as_list(item.get("recall_sources"))]]))
    return ExpertCandidate(
        code=code,
        name=name,
        market=str(item.get("market") or "cn"),
        score=max(0.0, min(100.0, score)),
        confidence=_score_confidence(score, default_confidence),
        stance="support",
        reason=reason,
        evidence_refs=[ref for ref in evidence_refs if ref],
        reason_dimensions=_as_list(item.get("reason_dimensions")),
        counter_evidence=_as_list(item.get("counter_evidence")),
        valid_until=_today_valid_until(1),
        refresh_policy="next_trading_day",
        raw={**item, "candidate_expert": expert, "candidate_dimension": dimension},
    )


def _raw_from_candidate(candidate: ExpertCandidate) -> Dict[str, Any]:
    payload = dict(candidate.raw or {})
    evidence_refs = [ref for ref in candidate.evidence_refs if ref]
    payload.update({
        "code": candidate.code,
        "name": candidate.name,
        "market": candidate.market,
        "source": payload.get("source") or (evidence_refs[0] if evidence_refs else candidate.dimension),
        "reason": candidate.reason,
        "signal_score": candidate.score,
        "candidate_expert": payload.get("candidate_expert"),
        "candidate_dimension": payload.get("candidate_dimension"),
        "candidate_confidence": candidate.confidence,
        "candidate_stance": candidate.stance,
        "evidence_refs": evidence_refs,
        "recall_sources": payload.get("recall_sources") or evidence_refs,
        "entry_reasons": payload.get("entry_reasons") or [candidate.reason],
    })
    if candidate.reason_dimensions:
        payload["reason_dimensions"] = candidate.reason_dimensions
    if candidate.counter_evidence:
        payload["counter_evidence"] = candidate.counter_evidence
    return payload


class CandidateExpertOrchestrator:
    """Run independent discovery experts and merge their candidate packets."""

    def __init__(
        self,
        *,
        timeout_s: Optional[float] = None,
        max_workers: int = 6,
        max_candidates_to_deep_dive: int = 8,
        min_per_expert: int = 1,
        max_per_expert: int = 4,
        max_theme_watch_items: int = 5,
    ) -> None:
        self.timeout_s = _resolve_timeout(timeout_s)
        self.max_workers = max(1, int(max_workers or 6))
        self.max_candidates_to_deep_dive = max(1, int(max_candidates_to_deep_dive or 8))
        self.min_per_expert = max(0, int(min_per_expert or 0))
        self.max_per_expert = max(1, int(max_per_expert or 4))
        self.max_theme_watch_items = max(0, int(max_theme_watch_items or 5))

    def discover(
        self,
        *,
        market: str = "cn",
        sector_names: Optional[Sequence[str]] = None,
        strategy_names: Optional[Sequence[str]] = None,
        limit: int = 8,
        tools: Dict[str, CandidateToolFn],
        seed_candidates_for_news: Optional[Iterable[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        effective_limit = max(1, min(int(limit or self.max_candidates_to_deep_dive), 20))
        provider_limit = min(50, max(effective_limit * 3, effective_limit))
        local_strategy_tasks: Dict[str, Callable[[], ExpertCandidatePacket]] = {
            "strategy_factor_expert": lambda: self._strategy_factor_packet(
                limit=provider_limit,
                strategy_names=strategy_names,
            ),
            "technical_candidate_expert": lambda: self._technical_packet(
                limit=provider_limit,
                strategy_names=strategy_names,
            ),
        }
        external_tasks: Dict[str, Callable[[], ExpertCandidatePacket]] = {
            "sector_theme_expert": lambda: self._sector_packet(
                sector_names=sector_names,
                limit=provider_limit,
                tools=tools,
            ),
            "news_event_expert": lambda: self._news_packet(
                limit=min(20, provider_limit),
                tools=tools,
                seed_candidates=seed_candidates_for_news,
            ),
            "sentiment_theme_expert": lambda: self._event_packet(
                market=market,
                limit=min(20, provider_limit),
                tools=tools,
            ),
            "fundamental_expert": lambda: self._fundamental_packet(
                limit=provider_limit,
                strategy_names=strategy_names,
                tools=tools,
            ),
            "capital_flow_expert": lambda: self._capital_packet(
                limit=provider_limit,
                tools=tools,
            ),
        }
        packets = [
            *self._run_sequential(local_strategy_tasks, runtime_group="local_strategy", timeout_s=self.timeout_s),
            *self._run_parallel(
                external_tasks,
                runtime_group="external_context",
                timeout_s=min(8.0, self.timeout_s),
            ),
        ]
        merged = self._merge_packets(packets, effective_limit)
        hard_exclusion = merged["hard_exclusion"]
        fallback_used = False
        discovery_steps = [packet.to_discovery_step() for packet in packets]
        discovery_steps.append({
            "source": "candidate_hard_exclusion",
            "status": "ok",
            "count": hard_exclusion.get("excluded_count", 0),
            "diagnostics": hard_exclusion,
        })
        candidates = merged["candidates"]
        if not candidates:
            fallback_fn = tools.get("fallback")
            fallback_candidates = fallback_fn(effective_limit) if fallback_fn else []
            candidates = list(fallback_candidates)
            fallback_used = True
            discovery_steps.append({"source": "fallback_seed_pool", "status": "ok", "count": len(candidates)})
        return {
            "status": "partial" if fallback_used else ("ok" if candidates else "partial"),
            "candidate_source": "fallback" if fallback_used else "expert_graph_discovery",
            "candidates": candidates,
            "candidate_count": len(candidates),
            "fallback_used": fallback_used,
            "expert_packets": [packet.model_dump(mode="json") for packet in packets],
            "themes": merged["themes"],
            "quality": merged["quality"],
            "hard_exclusion": hard_exclusion,
            "discovery_steps": discovery_steps,
            "capacity": {
                "max_candidates_to_deep_dive": effective_limit,
                "min_per_expert": self.min_per_expert,
                "max_per_expert": self.max_per_expert,
                "max_theme_watch_items": self.max_theme_watch_items,
                "soft_quotas": _default_soft_quotas(),
            },
        }

    def _run_parallel(
        self,
        tasks: Dict[str, Callable[[], ExpertCandidatePacket]],
        *,
        runtime_group: str,
        timeout_s: float,
    ) -> List[ExpertCandidatePacket]:
        started = time.time()
        packets_by_name: Dict[str, ExpertCandidatePacket] = {}
        if not tasks:
            return []
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(self.max_workers, len(tasks)))
        try:
            futures = {pool.submit(task): name for name, task in tasks.items()}
            done, pending = concurrent.futures.wait(
                futures,
                timeout=timeout_s,
                return_when=concurrent.futures.ALL_COMPLETED,
            )
            for future in pending:
                name = futures[future]
                future.cancel()
                packets_by_name[name] = ExpertCandidatePacket(
                    expert=name,
                    dimension=_dimension_from_expert(name),
                    status="timeout",
                    timeout_s=timeout_s,
                    errors=[f"{name} timeout after {timeout_s:.1f}s"],
                )
            for future in done:
                name = futures[future]
                try:
                    packets_by_name[name] = future.result()
                except Exception as exc:
                    packets_by_name[name] = ExpertCandidatePacket(
                        expert=name,
                        dimension=_dimension_from_expert(name),
                        status="failed",
                        errors=[str(exc)],
                    )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        ordered = [packets_by_name[name] for name in tasks if name in packets_by_name]
        elapsed_ms = int((time.time() - started) * 1000)
        for packet in ordered:
            packet.diagnostics.append({
                "source": "candidate_expert_runtime",
                "group": runtime_group,
                "status": packet.status,
                "duration_ms": elapsed_ms,
            })
        return ordered

    def _run_sequential(
        self,
        tasks: Dict[str, Callable[[], ExpertCandidatePacket]],
        *,
        runtime_group: str,
        timeout_s: float,
    ) -> List[ExpertCandidatePacket]:
        packets: List[ExpertCandidatePacket] = []
        for name, task in tasks.items():
            started = time.time()
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = pool.submit(task)
            try:
                packet = future.result(timeout=timeout_s)
            except concurrent.futures.TimeoutError:
                future.cancel()
                packet = ExpertCandidatePacket(
                    expert=name,
                    dimension=_dimension_from_expert(name),
                    status="timeout",
                    timeout_s=timeout_s,
                    errors=[f"{name} timeout after {timeout_s:.1f}s"],
                )
            except Exception as exc:
                packet = ExpertCandidatePacket(
                    expert=name,
                    dimension=_dimension_from_expert(name),
                    status="failed",
                    errors=[str(exc)],
                )
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            packet.diagnostics.append({
                "source": "candidate_expert_runtime",
                "group": runtime_group,
                "status": packet.status,
                "duration_ms": int((time.time() - started) * 1000),
            })
            packets.append(packet)
        return packets

    def _strategy_factor_packet(self, *, limit: int, strategy_names: Optional[Sequence[str]]) -> ExpertCandidatePacket:
        result = AlphaSiftCandidateProvider().discover(limit=limit, strategy_names=strategy_names)
        if result.get("status") == "empty" and strategy_names:
            fallback_result = AlphaSiftCandidateProvider().discover(limit=limit, strategy_names=None)
            if fallback_result.get("candidates"):
                fallback_result.setdefault("diagnostics", [])
                fallback_result["diagnostics"] = [
                    {
                        "source": "alphasift_strategy_filter",
                        "status": "fallback_to_all",
                        "requested": list(strategy_names),
                        "reason": "Requested strategy names did not match AlphaSift YAML strategies in auto expert discovery.",
                    },
                    *(fallback_result.get("diagnostics") or []),
                ]
                result = fallback_result
        candidates = [
            _candidate_from_raw(
                item,
                expert="strategy_factor_expert",
                dimension="strategy",
                default_reason="AlphaSift YAML 多因子策略候选。",
                evidence_prefix="alphasift",
                default_confidence=0.6,
            )
            for item in result.get("candidates") or []
            if item.get("code")
        ]
        return ExpertCandidatePacket(
            expert="strategy_factor_expert",
            dimension="strategy",
            status=_status_from_provider(result),
            data_quality=CandidateDataQuality(
                freshness="eod_current" if result.get("latest_date") else "unknown",
                as_of=result.get("latest_date"),
                source_chain=[{"provider": "alphasift", "db_path": result.get("db_path"), "strategies_dir": result.get("strategies_dir")}],
                warnings=[] if candidates else [str(result.get("error") or "AlphaSift produced no candidates")],
            ),
            candidates=candidates,
            diagnostics=result.get("diagnostics") or [],
            errors=[str(result.get("error"))] if result.get("error") else [],
        )

    def _technical_packet(self, *, limit: int, strategy_names: Optional[Sequence[str]]) -> ExpertCandidatePacket:
        result = SequoiaCandidateProvider().discover(limit=limit, strategy_names=strategy_names)
        candidates = [
            _candidate_from_raw(
                item,
                expert="technical_candidate_expert",
                dimension="technical",
                default_reason="Sequoia 技术形态/动量候选。",
                evidence_prefix="sequoia",
                default_confidence=0.62,
            )
            for item in result.get("candidates") or []
            if item.get("code")
        ]
        return ExpertCandidatePacket(
            expert="technical_candidate_expert",
            dimension="technical",
            status=_status_from_provider(result),
            data_quality=CandidateDataQuality(
                freshness="eod_current" if result.get("latest_date") else "unknown",
                as_of=result.get("latest_date"),
                source_chain=[{"provider": "sequoia", "db_path": result.get("db_path")}],
                warnings=[] if candidates else [str(result.get("error") or "Sequoia produced no candidates")],
            ),
            candidates=candidates,
            diagnostics=result.get("diagnostics") or [],
            errors=[str(result.get("error"))] if result.get("error") else [],
        )

    def _fundamental_packet(
        self,
        *,
        limit: int,
        strategy_names: Optional[Sequence[str]],
        tools: Optional[Dict[str, CandidateToolFn]] = None,
    ) -> ExpertCandidatePacket:
        result = FundamentalCandidateProvider().discover(limit=limit, strategy_names=strategy_names)
        diagnostics: List[Dict[str, Any]] = []
        raw_candidates: List[Dict[str, Any]] = [
            item
            for item in (result.get("candidates") or [])
            if isinstance(item, dict)
        ]
        diagnostics.extend(result.get("diagnostics") or [])
        for source_name, tool_name in (
            ("tushare_daily_basic", "tushare_daily_basic"),
        ):
            fn = (tools or {}).get(tool_name)
            if fn is None:
                diagnostics.append({"source": source_name, "status": "unavailable", "reason": f"{tool_name} tool not provided"})
                continue
            try:
                query_limit = min(30, max(limit * 3, 10)) if source_name == "tushare_daily_basic" else min(20, max(limit * 2, 8))
                tool_result = fn(query_limit)
            except Exception as exc:
                diagnostics.append({"source": source_name, "status": "failed", "error": str(exc)})
                continue
            status = str(tool_result.get("status") or "empty")
            items = tool_result.get("items") if isinstance(tool_result.get("items"), list) else []
            diagnostics.append({
                "source": source_name,
                "status": status,
                "count": len(items),
                "source_chain": tool_result.get("source_chain") or [],
                "errors": tool_result.get("errors") or ([] if not tool_result.get("error") else [tool_result.get("error")]),
            })
            raw_candidates.extend(_fundamental_candidates_from_tushare(source_name, items, limit=limit))
        raw_candidates = _merge_raw_fundamental_candidates(raw_candidates, limit)
        candidates = [
            _candidate_from_raw(
                item,
                expert="fundamental_expert",
                dimension="fundamental",
                default_reason="基本面质量/成长/估值预计算候选。",
                evidence_prefix="fundamental",
                default_confidence=0.58,
            )
            for item in raw_candidates
            if item.get("code")
        ]
        return ExpertCandidatePacket(
            expert="fundamental_expert",
            dimension="fundamental",
            status="ok" if candidates else _status_from_provider(result),
            data_quality=CandidateDataQuality(
                freshness="filing_or_weekly",
                as_of=result.get("latest_period") or result.get("updated_at"),
                source_chain=[{"provider": "fundamental_candidate_snapshot + tushare_fundamental_tools", "db_path": result.get("db_path"), "table": result.get("table")}],
                warnings=[] if candidates else [str(result.get("error") or "Fundamental candidate snapshot produced no candidates")],
            ),
            candidates=candidates,
            diagnostics=diagnostics,
            errors=[str(result.get("error"))] if result.get("error") else [],
        )

    def _capital_packet(self, *, limit: int, tools: Dict[str, CandidateToolFn]) -> ExpertCandidatePacket:
        diagnostics: List[Dict[str, Any]] = []
        raw_candidates: List[Dict[str, Any]] = []
        status_values: List[str] = []

        for source_name, tool_name in (
            ("tushare_moneyflow_ths", "tushare_moneyflow_ths"),
            ("tushare_moneyflow_dc", "tushare_moneyflow_dc"),
            ("tushare_dragon_tiger_list", "tushare_dragon_tiger_list"),
            ("tushare_dragon_tiger_inst", "tushare_dragon_tiger_inst"),
            ("tushare_limit_list_ths", "tushare_limit_list_ths"),
            ("tushare_limit_list_d", "tushare_limit_list_d"),
            ("tushare_limit_step", "tushare_limit_step"),
            ("tushare_hot_rank", "tushare_hot_rank"),
            ("limit_up_pool", "stockapi_limit_up_pool"),
            ("popularity_rank", "stockapi_popularity_rank"),
            ("hot_money_activity", "stockapi_hot_money_activity"),
        ):
            fn = tools.get(tool_name)
            if fn is None:
                diagnostics.append({"source": source_name, "status": "unavailable", "reason": f"{tool_name} tool not provided"})
                continue
            try:
                result = fn(min(30, max(limit, 5)))
            except Exception as exc:
                diagnostics.append({"source": source_name, "status": "failed", "error": str(exc)})
                status_values.append("failed")
                continue

            status = str(result.get("status") or "empty")
            status_values.append(status)
            items = result.get("items") if isinstance(result.get("items"), list) else []
            diagnostics.append({
                "source": source_name,
                "status": status,
                "count": len(items),
                "source_chain": result.get("source_chain") or [],
                "errors": result.get("errors") or ([] if not result.get("error") else [result.get("error")]),
                "degraded": bool(result.get("degraded")),
            })
            raw_candidates.extend(_capital_candidates_from_items(source_name, items, limit=limit))

        merged = _merge_raw_capital_candidates(raw_candidates, limit)
        candidates = [
            _candidate_from_raw(
                item,
                expert="capital_flow_expert",
                dimension="capital",
                default_reason="资金活跃度/涨停池/人气榜候选。",
                evidence_prefix="capital_flow",
                default_confidence=0.5,
            )
            for item in merged
            if item.get("code")
        ]
        if candidates:
            packet_status = "ok" if any(status in {"ok", "partial"} for status in status_values) else "partial"
        elif any(status in {"failed", "timeout", "error"} for status in status_values):
            packet_status = "failed"
        elif any(status in {"unavailable", "not_supported"} for status in status_values):
            packet_status = "unavailable"
        else:
            packet_status = "empty"
        warnings = [] if candidates else ["Capital-flow discovery produced no stock candidates"]
        if any(item.get("degraded") for item in diagnostics):
            warnings.append("Some capital-flow candidate sources used degraded fallback data")
        return ExpertCandidatePacket(
            expert="capital_flow_expert",
            dimension="capital",
            status=packet_status,
            data_quality=CandidateDataQuality(
                freshness="intraday",
                source_chain=[{"provider": "tushare/stockapi/akshare capital candidate tools"}],
                warnings=warnings,
            ),
            candidates=candidates,
            diagnostics=diagnostics,
            errors=[str(error) for item in diagnostics for error in (item.get("errors") or [])][:8],
        )

    def _sector_packet(
        self,
        *,
        sector_names: Optional[Sequence[str]],
        limit: int,
        tools: Dict[str, CandidateToolFn],
    ) -> ExpertCandidatePacket:
        top_sector_names = tools["top_sector_names"]
        fetch_sector_constituents = tools["fetch_sector_constituents"]
        sectors = [str(name).strip() for name in (sector_names or []) if str(name or "").strip()]
        diagnostics: List[Dict[str, Any]] = []
        raw_candidates: List[Dict[str, Any]] = []

        ths_member_fn = tools.get("tushare_ths_member")
        for source_name, tool_name in (
            ("tushare_moneyflow_ind_ths", "tushare_moneyflow_ind_ths"),
            ("tushare_moneyflow_cnt_ths", "tushare_moneyflow_cnt_ths"),
            ("tushare_moneyflow_ind_dc", "tushare_moneyflow_ind_dc"),
        ):
            fn = tools.get(tool_name)
            if fn is None:
                diagnostics.append({"source": source_name, "status": "unavailable", "reason": f"{tool_name} tool not provided"})
                continue
            try:
                result = fn(min(10, max(limit, 5)))
            except Exception as exc:
                diagnostics.append({"source": source_name, "status": "failed", "error": str(exc)})
                continue
            status = str(result.get("status") or "empty")
            items = result.get("items") if isinstance(result.get("items"), list) else []
            diagnostics.append({
                "source": source_name,
                "status": status,
                "count": len(items),
                "source_chain": result.get("source_chain") or [],
                "errors": result.get("errors") or ([] if not result.get("error") else [result.get("error")]),
            })
            if source_name in {"tushare_moneyflow_ind_ths", "tushare_moneyflow_cnt_ths"} and ths_member_fn is not None:
                raw_candidates.extend(
                    _sector_candidates_from_tushare_boards(
                        source_name,
                        items,
                        member_fn=ths_member_fn,
                        diagnostics=diagnostics,
                        limit=limit,
                    )
                )

        if not sectors:
            sectors = top_sector_names(5)
            diagnostics.append({"source": "get_sector_rankings", "status": "ok" if sectors else "empty", "sectors": sectors})
        per_sector_limit = max(2, min(10, limit))
        for sector in sectors[:5]:
            result = fetch_sector_constituents(sector, per_sector_limit, include_diagnostics=True)
            if isinstance(result, tuple):
                sector_candidates, sector_diagnostics = result
            else:
                sector_candidates, sector_diagnostics = result, []
            diagnostics.append({
                "source": "sector_constituents",
                "sector": sector,
                "status": "ok" if sector_candidates else "empty",
                "count": len(sector_candidates),
                "diagnostics": sector_diagnostics,
            })
            raw_candidates.extend(sector_candidates)
        candidates = [
            _candidate_from_raw(
                item,
                expert="sector_theme_expert",
                dimension="sector_theme",
                default_reason="强势板块成分候选。",
                evidence_prefix="sector",
                default_confidence=0.5,
            )
            for item in raw_candidates
            if item.get("code")
        ]
        return ExpertCandidatePacket(
            expert="sector_theme_expert",
            dimension="sector_theme",
            status="ok" if candidates else "empty",
            data_quality=CandidateDataQuality(
                freshness="intraday",
                source_chain=[{"provider": "tushare_moneyflow/ths_member + akshare/eastmoney_sector_constituents", "sectors": sectors}],
                warnings=[] if candidates else ["Sector constituents produced no candidates"],
            ),
            candidates=candidates,
            diagnostics=diagnostics,
        )

    def _news_packet(
        self,
        *,
        limit: int,
        tools: Dict[str, CandidateToolFn],
        seed_candidates: Optional[Iterable[Dict[str, Any]]],
    ) -> ExpertCandidatePacket:
        result = tools["discover_news_momentum"](limit, seed_candidates)
        diagnostics: List[Dict[str, Any]] = [*(result.get("diagnostics") or []), *(_as_dict_list(result.get("queries")))]
        raw_candidates: List[Dict[str, Any]] = [
            item
            for item in (result.get("candidates") or [])
            if isinstance(item, dict)
        ]
        for source_name, tool_name in (
            ("tushare_announcements", "tushare_announcements"),
            ("tushare_stock_alerts", "tushare_stock_alerts"),
            ("tushare_stock_shock", "tushare_stock_shock"),
            ("tushare_share_float", "tushare_share_float"),
            ("tushare_holder_trade", "tushare_holder_trade"),
            ("tushare_repurchase", "tushare_repurchase"),
        ):
            fn = tools.get(tool_name)
            if fn is None:
                diagnostics.append({"source": source_name, "status": "unavailable", "reason": f"{tool_name} tool not provided"})
                continue
            try:
                tool_result = fn(min(20, max(limit * 2, 8)))
            except Exception as exc:
                diagnostics.append({"source": source_name, "status": "failed", "error": str(exc)})
                continue
            status = str(tool_result.get("status") or "empty")
            items = tool_result.get("items") if isinstance(tool_result.get("items"), list) else []
            diagnostics.append({
                "source": source_name,
                "status": status,
                "count": len(items),
                "source_chain": tool_result.get("source_chain") or [],
                "errors": tool_result.get("errors") or ([] if not tool_result.get("error") else [tool_result.get("error")]),
            })
            raw_candidates.extend(_event_candidates_from_tushare(source_name, items, limit=limit))
        raw_candidates = _merge_raw_event_candidates(raw_candidates, limit)
        candidates = [
            _candidate_from_raw(
                item,
                expert="news_event_expert",
                dimension="news_event",
                default_reason="公司级新闻/公告事件候选。",
                evidence_prefix="news_momentum",
                default_confidence=0.56,
            )
            for item in raw_candidates
            if item.get("code")
        ]
        return ExpertCandidatePacket(
            expert="news_event_expert",
            dimension="news_event",
            status="ok" if candidates else _status_from_provider(result),
            data_quality=CandidateDataQuality(
                freshness="recent",
                source_chain=[{"provider": "search/news_momentum + tushare_event_tools"}],
                warnings=[] if candidates else ["News momentum produced no stock candidates"],
            ),
            candidates=candidates,
            diagnostics=diagnostics,
        )

    def _event_packet(self, *, market: str, limit: int, tools: Dict[str, CandidateToolFn]) -> ExpertCandidatePacket:
        result = tools["discover_event_impact"](market, limit)
        candidates = [
            _candidate_from_raw(
                item,
                expert="sentiment_theme_expert",
                dimension="sentiment_theme",
                default_reason="事件传导验证后的主题成分候选。",
                evidence_prefix="event_impact",
                default_confidence=0.5,
            )
            for item in result.get("candidates") or []
            if item.get("code")
        ]
        themes: List[ThemeWatchItem] = []
        for event in result.get("events") or []:
            for theme in (event.get("watch_themes") or [])[: self.max_theme_watch_items]:
                themes.append(ThemeWatchItem(
                    theme=str(theme),
                    event_title=str(event.get("title") or ""),
                    status=str(event.get("maturity") or "watch"),
                    reason="宏观/情绪事件观察，未验证时不得直接推出个股。",
                    evidence_refs=[str(event.get("event_id") or "")],
                    confidence=0.4,
                ))
                if len(themes) >= self.max_theme_watch_items:
                    break
            if len(themes) >= self.max_theme_watch_items:
                break
        return ExpertCandidatePacket(
            expert="sentiment_theme_expert",
            dimension="sentiment_theme",
            status=_status_from_provider(result),
            data_quality=CandidateDataQuality(
                freshness="recent",
                source_chain=[{"provider": "search/event_impact"}],
                warnings=[] if candidates else ["Event impact has no validated stock candidates; themes kept as watch-only"],
            ),
            themes=themes,
            candidates=candidates,
            diagnostics=[*(result.get("diagnostics") or []), *(_as_dict_list(result.get("queries")))],
        )

    def _empty_packet(self, *, expert: str, dimension: str, reason: str) -> ExpertCandidatePacket:
        return ExpertCandidatePacket(
            expert=expert,
            dimension=dimension,
            status="empty",
            data_quality=CandidateDataQuality(warnings=[reason]),
            diagnostics=[{"source": expert, "status": "not_wired", "reason": reason}],
        )

    def _merge_packets(self, packets: List[ExpertCandidatePacket], limit: int) -> Dict[str, Any]:
        by_code: Dict[str, Dict[str, Any]] = {}
        for packet in packets:
            for candidate in packet.candidates:
                if candidate.stance == "invalid":
                    continue
                raw = _raw_from_candidate(candidate)
                raw["candidate_experts"] = [packet.expert]
                raw["candidate_dimensions"] = [packet.dimension]
                raw["expert_confidences"] = {packet.expert: candidate.confidence}
                code = candidate.code
                if code not in by_code:
                    by_code[code] = raw
                    continue
                current = by_code[code]
                current["signal_score"] = max(float(current.get("signal_score") or 0), candidate.score)
                current["candidate_experts"] = list(dict.fromkeys([*(current.get("candidate_experts") or []), packet.expert]))
                current["candidate_dimensions"] = list(dict.fromkeys([*(current.get("candidate_dimensions") or []), packet.dimension]))
                confidences = dict(current.get("expert_confidences") or {})
                confidences[packet.expert] = candidate.confidence
                current["expert_confidences"] = confidences
                sources = list(current.get("recall_sources") or [])
                for ref in candidate.evidence_refs:
                    if ref and ref not in sources:
                        sources.append(ref)
                current["recall_sources"] = sources
                entry_reasons = list(current.get("entry_reasons") or [])
                if candidate.reason and candidate.reason not in entry_reasons:
                    entry_reasons.append(candidate.reason)
                current["entry_reasons"] = entry_reasons
                counter_evidence = list(current.get("counter_evidence") or [])
                for evidence in candidate.counter_evidence:
                    if evidence not in counter_evidence:
                        counter_evidence.append(evidence)
                if counter_evidence:
                    current["counter_evidence"] = counter_evidence
                if len(current["candidate_experts"]) > 1:
                    current["source"] = "multi_expert_recall"
                    current["reason"] = f"多专家候选共振：{'、'.join(current['candidate_dimensions'])}。"
        ranked = list(by_code.values())
        ranked, hard_exclusion = apply_hard_exclusion(ranked)
        for item in ranked:
            supporting_confidences = [float(value) for value in (item.get("expert_confidences") or {}).values()]
            consensus_bonus = min(15.0, sum(value * 5.0 for value in supporting_confidences))
            item["signal_score"] = round(max(0.0, min(100.0, float(item.get("signal_score") or 0) + consensus_bonus)), 2)
            item["consensus_bonus"] = round(consensus_bonus, 2)
            item["lifecycle_status"] = "new"
            item["mixed_evidence"] = bool(item.get("counter_evidence"))
        ranked.sort(
            key=lambda item: (
                float(item.get("signal_score") or 0),
                len(item.get("candidate_experts") or []),
                max([float(value) for value in (item.get("expert_confidences") or {"_": 0}).values()] or [0]),
                str(item.get("code") or ""),
            ),
            reverse=True,
        )
        selected = self._select_with_capacity(ranked, limit)
        themes = []
        for packet in packets:
            themes.extend([theme.model_dump(mode="json") for theme in packet.themes])
        return {
            "candidates": selected,
            "themes": themes[: self.max_theme_watch_items],
            "quality": _candidate_quality_summary(selected, packets, hard_exclusion),
            "hard_exclusion": hard_exclusion,
        }

    def _select_with_capacity(self, ranked: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        selected: List[Dict[str, Any]] = []
        selected_codes: set[str] = set()
        per_expert_count: Dict[str, int] = {}

        def can_add(item: Dict[str, Any], *, enforce_cap: bool) -> bool:
            code = str(item.get("code") or "")
            if not code or code in selected_codes or len(selected) >= limit:
                return False
            if enforce_cap:
                experts = item.get("candidate_experts") or []
                if experts and all(per_expert_count.get(str(expert), 0) >= self.max_per_expert for expert in experts):
                    return False
            return True

        expert_order = [
            "strategy_factor_expert",
            "technical_candidate_expert",
            "sector_theme_expert",
            "capital_flow_expert",
            "news_event_expert",
            "sentiment_theme_expert",
            "fundamental_expert",
        ]
        for expert in expert_order:
            if self.min_per_expert <= 0:
                break
            for item in ranked:
                if expert not in (item.get("candidate_experts") or []):
                    continue
                if not can_add(item, enforce_cap=False):
                    continue
                selected.append(item)
                selected_codes.add(str(item.get("code")))
                for owner in item.get("candidate_experts") or []:
                    per_expert_count[str(owner)] = per_expert_count.get(str(owner), 0) + 1
                break
        for item in ranked:
            if not can_add(item, enforce_cap=True):
                continue
            selected.append(item)
            selected_codes.add(str(item.get("code")))
            for owner in item.get("candidate_experts") or []:
                per_expert_count[str(owner)] = per_expert_count.get(str(owner), 0) + 1
            if len(selected) >= limit:
                break
        selected.sort(key=lambda item: -float(item.get("signal_score") or 0))
        return selected


def _status_from_provider(result: Dict[str, Any]) -> str:
    status = str(result.get("status") or "empty").lower()
    if status in {"ok", "partial", "empty", "failed", "timeout", "unavailable"}:
        return status
    return "failed"


def _dimension_from_expert(expert: str) -> str:
    mapping = {
        "strategy_factor_expert": "strategy",
        "technical_candidate_expert": "technical",
        "sector_theme_expert": "sector_theme",
        "capital_flow_expert": "capital",
        "news_event_expert": "news_event",
        "sentiment_theme_expert": "sentiment_theme",
        "fundamental_expert": "fundamental",
    }
    return mapping.get(expert, "candidate")


def _resolve_timeout(value: Optional[float]) -> float:
    raw = value
    if raw is None:
        env_value = os.getenv("AGENT_CANDIDATE_EXPERT_TIMEOUT_SECONDS")
        if env_value not in {None, ""}:
            try:
                raw = float(str(env_value).strip())
            except ValueError:
                raw = None
    if raw is None:
        raw = 20.0
    return max(1.0, float(raw))


def _as_dict_list(value: Any) -> List[Dict[str, Any]]:
    return [item for item in (value or []) if isinstance(item, dict)]


def _event_candidates_from_tushare(source_name: str, rows: List[Dict[str, Any]], *, limit: int) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for rank, row in enumerate(rows[: max(1, limit * 2)], start=1):
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or row.get("ts_code") or row.get("stock_code") or "").strip()
        if "." in code:
            code = code.split(".", 1)[0]
        if not (code.isdigit() and len(code) == 6):
            continue
        event_label = _event_label(source_name, row)
        polarity = _event_polarity(source_name, row)
        score = _event_candidate_score(source_name, row, rank, polarity)
        reason = _event_candidate_reason(source_name, event_label, polarity)
        payload = {
            "code": code,
            "name": str(row.get("name") or code).strip(),
            "source": f"news_event:{source_name}",
            "reason": reason,
            "signal_score": score,
            "strategy_tags": ["news_event", source_name, polarity],
            "metrics": _event_candidate_metrics(source_name, row, rank, polarity),
            "raw_source_item": row,
            "reason_dimensions": _event_reason_dimensions(source_name, row, event_label, polarity),
        }
        if polarity in {"negative", "risk"}:
            payload["counter_evidence"] = [{
                "dimension": "event_risk",
                "label": event_label,
                "detail": reason,
            }]
        candidates.append(payload)
    return candidates


def _merge_raw_event_candidates(candidates: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    by_code: Dict[str, Dict[str, Any]] = {}
    for item in candidates:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        if code not in by_code:
            by_code[code] = dict(item)
            continue
        current = by_code[code]
        current["signal_score"] = round(max(float(current.get("signal_score") or 0), float(item.get("signal_score") or 0)) + 2.0, 2)
        current["source"] = "news_event:multi_source"
        current["reason"] = "多个结构化消息/事件来源共振。"
        current_sources = list(current.get("recall_sources") or [current.get("source")])
        for source in [item.get("source")]:
            if source and source not in current_sources:
                current_sources.append(source)
        current["recall_sources"] = current_sources
        current["reason_dimensions"] = list(current.get("reason_dimensions") or []) + list(item.get("reason_dimensions") or [])
        current["counter_evidence"] = list(current.get("counter_evidence") or []) + list(item.get("counter_evidence") or [])
    merged = list(by_code.values())
    merged.sort(key=lambda item: (float(item.get("signal_score") or 0), str(item.get("code") or "")), reverse=True)
    return merged[: max(1, int(limit or 1))]


def _event_label(source_name: str, row: Dict[str, Any]) -> str:
    if source_name == "tushare_announcements":
        return str(row.get("title") or "公司公告").strip()
    if source_name == "tushare_stock_alerts":
        return str(row.get("type") or "风险提示").strip()
    if source_name == "tushare_stock_shock":
        return str(row.get("reason") or "异常波动").strip()
    if source_name == "tushare_share_float":
        return "限售解禁"
    if source_name == "tushare_holder_trade":
        in_de = str(row.get("in_de") or "").upper()
        return "股东增持" if in_de == "IN" else "股东减持" if in_de == "DE" else "股东增减持"
    if source_name == "tushare_repurchase":
        return str(row.get("proc") or "股份回购").strip()
    return "结构化事件"


def _event_polarity(source_name: str, row: Dict[str, Any]) -> str:
    text = " ".join(str(row.get(key) or "") for key in ("title", "type", "reason", "proc", "in_de")).lower()
    if source_name in {"tushare_stock_alerts", "tushare_stock_shock", "tushare_share_float"}:
        return "risk"
    if source_name == "tushare_holder_trade":
        in_de = str(row.get("in_de") or "").upper()
        return "positive" if in_de == "IN" else "negative" if in_de == "DE" else "neutral"
    if source_name == "tushare_repurchase":
        return "positive"
    if any(token in text for token in ("减持", "风险", "处罚", "问询", "警示", "退市", "诉讼")):
        return "negative"
    if any(token in text for token in ("回购", "增持", "预增", "中标", "签订", "分红", "业绩快报")):
        return "positive"
    return "neutral"


def _event_candidate_score(source_name: str, row: Dict[str, Any], rank: int, polarity: str) -> float:
    base = 58.0 + max(0.0, 8.0 - min(rank, 8) * 0.6)
    amount = _safe_float(row.get("amount") or row.get("change_vol") or row.get("float_share")) or 0.0
    base += max(min(abs(amount) / 100_000_000, 8.0), 0.0)
    if source_name == "tushare_repurchase":
        base += 6.0
    elif source_name == "tushare_announcements":
        base += 2.0
    elif polarity in {"negative", "risk"}:
        base -= 8.0
    return round(max(35.0, min(82.0, base)), 2)


def _event_candidate_reason(source_name: str, event_label: str, polarity: str) -> str:
    prefix = {
        "tushare_announcements": "TuShare 公司公告",
        "tushare_stock_alerts": "TuShare 风险提示",
        "tushare_stock_shock": "TuShare 异常波动",
        "tushare_share_float": "TuShare 限售解禁",
        "tushare_holder_trade": "TuShare 股东增减持",
        "tushare_repurchase": "TuShare 回购事件",
    }.get(source_name, "TuShare 结构化事件")
    suffix = "偏利好，但仍需验证价格和资金承接。" if polarity == "positive" else "属于风险或反证，进入候选仅用于后续审查。" if polarity in {"negative", "risk"} else "需要结合新闻和行情确认方向。"
    return f"{prefix}「{event_label}」：{suffix}"


def _event_candidate_metrics(source_name: str, row: Dict[str, Any], rank: int, polarity: str) -> Dict[str, Any]:
    keys = (
        "ann_date",
        "trade_date",
        "start_date",
        "end_date",
        "float_date",
        "title",
        "type",
        "reason",
        "period",
        "holder_name",
        "change_vol",
        "change_ratio",
        "float_share",
        "float_ratio",
        "amount",
        "proc",
        "url",
    )
    metrics = {key: row.get(key) for key in keys if row.get(key) not in (None, "", [], {})}
    metrics["source_name"] = source_name
    metrics["source_rank"] = rank
    metrics["polarity"] = polarity
    return metrics


def _event_reason_dimensions(source_name: str, row: Dict[str, Any], event_label: str, polarity: str) -> List[Dict[str, str]]:
    parts = []
    for key, label in (
        ("ann_date", "公告日"),
        ("trade_date", "交易日"),
        ("float_date", "解禁日"),
        ("holder_name", "股东"),
        ("change_ratio", "变动比例"),
        ("amount", "金额"),
        ("float_ratio", "解禁比例"),
    ):
        value = row.get(key)
        if value not in (None, ""):
            parts.append(f"{label}={_short_metric(value)}")
    detail = f"{event_label}；方向={polarity}"
    if parts:
        detail += "；" + "；".join(parts[:5])
    return [{"dimension": "news_event", "label": "结构化事件", "detail": detail}]


def _fundamental_candidates_from_tushare(source_name: str, rows: List[Dict[str, Any]], *, limit: int) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for rank, row in enumerate(rows[: max(1, limit * 2)], start=1):
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or row.get("ts_code") or row.get("stock_code") or "").strip()
        if "." in code:
            code = code.split(".", 1)[0]
        if not (code.isdigit() and len(code) == 6):
            continue
        metrics = {
            "source_name": source_name,
            "rank": rank,
            "trade_date": row.get("trade_date") or row.get("ann_date") or row.get("end_date"),
            "close": row.get("close"),
            "turnover_rate": row.get("turnover_rate"),
            "pe_ttm": row.get("pe_ttm") or row.get("pe"),
            "pb": row.get("pb"),
            "roe": row.get("roe"),
            "grossprofit_margin": row.get("grossprofit_margin"),
            "netprofit_margin": row.get("netprofit_margin"),
            "revenue": row.get("revenue"),
            "yoy_net_profit": row.get("yoy_net_profit"),
            "cash_div": row.get("cash_div"),
            "cash_div_tax": row.get("cash_div_tax"),
            "summary": row.get("summary") or row.get("change_reason") or row.get("perf_summary"),
        }
        if source_name == "tushare_daily_basic":
            signal_score = _fundamental_daily_basic_score(row, rank)
            reason = "TuShare 日度估值/换手指标靠前，适合做基本面候选快照。"
            strategy_tags = ["fundamental", "daily_basic", "valuation"]
        elif source_name == "tushare_financial_indicators":
            signal_score = _fundamental_quality_score(row, rank)
            reason = "TuShare 财务指标显示质量/成长/杠杆维度较优。"
            strategy_tags = ["fundamental", "fina_indicator", "quality"]
        elif source_name in {"tushare_forecast", "tushare_express"}:
            signal_score = _fundamental_growth_score(row, rank)
            reason = "TuShare 业绩预告/快报显示成长或业绩修正信号。"
            strategy_tags = ["fundamental", source_name, "growth"]
        elif source_name == "tushare_dividend":
            signal_score = _fundamental_dividend_score(row, rank)
            reason = "TuShare 分红送股记录可用于价值与回报视角的基本面候选。"
            strategy_tags = ["fundamental", "dividend", "value"]
        else:
            signal_score = 55.0
            reason = "TuShare 基本面结构化数据候选。"
            strategy_tags = ["fundamental", source_name]
        candidates.append({
            "code": code,
            "name": str(row.get("name") or code).strip(),
            "source": f"fundamental:{source_name}",
            "reason": reason,
            "signal_score": round(max(0.0, min(96.0, signal_score)), 2),
            "strategy_tags": strategy_tags,
            "metrics": {
                key: value
                for key, value in _sanitize_json_like(metrics).items()
                if value not in (None, "", [], {})
            },
            "raw_source_item": _sanitize_json_like(row),
            "reason_dimensions": _sanitize_json_like(_fundamental_reason_dimensions(source_name, row)),
        })
    return candidates


def _merge_raw_fundamental_candidates(candidates: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    by_code: Dict[str, Dict[str, Any]] = {}
    for item in candidates:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        if code not in by_code:
            by_code[code] = dict(item)
            continue
        current = by_code[code]
        current["signal_score"] = round(max(float(current.get("signal_score") or 0), float(item.get("signal_score") or 0)) + 3.0, 2)
        current["source"] = "fundamental:multi_source"
        current["reason"] = "多个基本面结构化来源共振。"
        current_sources = list(current.get("recall_sources") or [current.get("source")])
        for source in [item.get("source")]:
            if source and source not in current_sources:
                current_sources.append(source)
        current["recall_sources"] = current_sources
        metrics = dict(current.get("metrics") or {})
        metrics[str(item.get("source") or "fundamental")] = item.get("metrics") or {}
        current["metrics"] = metrics
        current["reason_dimensions"] = _fundamental_reason_dimensions(current.get("source"), current)
    merged = list(by_code.values())
    merged.sort(key=lambda item: (float(item.get("signal_score") or 0), str(item.get("code") or "")), reverse=True)
    return merged[: max(1, int(limit or 1))]


def _fundamental_daily_basic_score(row: Dict[str, Any], rank: int) -> float:
    pe = _safe_float(row.get("pe_ttm") or row.get("pe")) or 0.0
    pb = _safe_float(row.get("pb")) or 0.0
    turnover = _safe_float(row.get("turnover_rate")) or 0.0
    mv = _safe_float(row.get("total_mv")) or 0.0
    score = 58.0 + max(0.0, 10.0 - min(rank, 10) * 0.7) + max(min(30.0 - pe, 12.0), -12.0) * 0.4 + max(min(5.0 - pb, 4.0), -4.0) * 1.5 + max(min(turnover, 8.0), 0.0) * 0.5 + max(min(mv / 100_000_000, 8.0), 0.0) * 0.3
    return score


def _fundamental_quality_score(row: Dict[str, Any], rank: int) -> float:
    roe = _safe_float(row.get("roe")) or 0.0
    gross = _safe_float(row.get("grossprofit_margin") or row.get("gross_margin")) or 0.0
    net = _safe_float(row.get("netprofit_margin") or row.get("net_margin")) or 0.0
    debt = _safe_float(row.get("debt_to_assets")) or 0.0
    score = 60.0 + max(0.0, 10.0 - min(rank, 10) * 0.8) + max(min(roe, 30.0), 0.0) * 0.8 + max(min(gross, 60.0), 0.0) * 0.2 + max(min(net, 30.0), 0.0) * 0.3 - max(min(debt - 50.0, 30.0), 0.0) * 0.4
    return score


def _fundamental_growth_score(row: Dict[str, Any], rank: int) -> float:
    yoy_profit = _safe_float(row.get("yoy_net_profit") or row.get("p_change_min")) or 0.0
    revenue = _safe_float(row.get("revenue")) or 0.0
    summary = str(row.get("summary") or row.get("change_reason") or row.get("perf_summary") or "").strip()
    score = 57.0 + max(0.0, 9.0 - min(rank, 9) * 0.8) + max(min(yoy_profit, 80.0), -20.0) * 0.4 + max(min(revenue / 100_000_000, 8.0), 0.0) * 0.5
    if summary:
        score += 2.0
    return score


def _fundamental_dividend_score(row: Dict[str, Any], rank: int) -> float:
    cash_div = _safe_float(row.get("cash_div")) or 0.0
    cash_div_tax = _safe_float(row.get("cash_div_tax")) or 0.0
    score = 56.0 + max(0.0, 8.0 - min(rank, 8) * 0.6) + max(min(cash_div / 10.0, 12.0), 0.0) + max(min(cash_div_tax / 10.0, 4.0), 0.0)
    return score


def _fundamental_reason_dimensions(source_name: str, row: Dict[str, Any]) -> List[Dict[str, str]]:
    parts: List[str] = []
    for key, label in (
        ("roe", "ROE"),
        ("grossprofit_margin", "毛利率"),
        ("netprofit_margin", "净利率"),
        ("pe_ttm", "PE(TTM)"),
        ("pb", "PB"),
        ("turnover_rate", "换手率"),
        ("yoy_net_profit", "归母净利同比"),
        ("cash_div", "现金分红"),
    ):
        value = row.get(key)
        if value not in (None, ""):
            parts.append(f"{label}={_short_metric(value)}")
    label = {
        "tushare_daily_basic": "日度估值",
        "tushare_financial_indicators": "财务指标",
        "tushare_forecast": "业绩预告",
        "tushare_express": "业绩快报",
        "tushare_dividend": "分红送股",
    }.get(source_name or "", "基本面")
    detail = f"{label}候选：" + "；".join(parts[:5])
    return [{"dimension": "fundamental", "label": label, "detail": detail}]


def _sector_candidates_from_tushare_boards(
    source_name: str,
    board_items: List[Dict[str, Any]],
    *,
    member_fn: CandidateToolFn,
    diagnostics: List[Dict[str, Any]],
    limit: int,
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    seen_codes: set[str] = set()
    per_board_limit = max(2, min(8, limit))
    for board_rank, board in enumerate(board_items[: max(1, min(5, limit))], start=1):
        if not isinstance(board, dict):
            continue
        board_code = str(board.get("ts_code") or "").strip()
        board_name = str(board.get("name") or board.get("industry") or board_code).strip()
        if not board_code:
            continue
        try:
            member_result = member_fn(board_code, per_board_limit)
        except Exception as exc:
            diagnostics.append({
                "source": "tushare_ths_member",
                "status": "failed",
                "board": board_name,
                "ts_code": board_code,
                "error": str(exc),
            })
            continue
        status = str(member_result.get("status") or "empty")
        members = member_result.get("items") if isinstance(member_result.get("items"), list) else []
        diagnostics.append({
            "source": "tushare_ths_member",
            "status": status,
            "board": board_name,
            "ts_code": board_code,
            "count": len(members),
            "source_chain": member_result.get("source_chain") or [],
            "errors": member_result.get("errors") or [],
        })
        for member_rank, member in enumerate(members[:per_board_limit], start=1):
            if not isinstance(member, dict):
                continue
            code = str(member.get("code") or "").strip()
            if not (code.isdigit() and len(code) == 6) or code in seen_codes:
                continue
            seen_codes.add(code)
            candidates.append({
                "code": code,
                "name": str(member.get("name") or code).strip(),
                "source": f"sector_theme:{source_name}",
                "reason": f"TuShare 板块资金流入靠前主题「{board_name}」成分股，需后续验证个股资金承接和价格结构。",
                "signal_score": _sector_candidate_score(board, board_rank, member_rank),
                "strategy_tags": ["sector_theme", source_name, "tushare_board_member"],
                "metrics": _sector_candidate_metrics(source_name, board, member, board_rank, member_rank),
                "raw_source_item": {"board": board, "member": member},
                "reason_dimensions": _sector_reason_dimensions(source_name, board, member),
            })
    return candidates


def _sector_candidate_score(board: Dict[str, Any], board_rank: int, member_rank: int) -> float:
    rank_bonus = max(0.0, 10.0 - min(board_rank, 10) * 0.8) + max(0.0, 5.0 - min(member_rank, 5) * 0.6)
    net_inflow = _safe_float(board.get("net_inflow")) or 0.0
    change_ratio = _safe_float(board.get("change_ratio")) or 0.0
    lead_change = _safe_float(board.get("lead_stock_pct_change")) or 0.0
    score = 55.0 + rank_bonus + max(min(net_inflow / 1_000_000_000 * 3.0, 12.0), -12.0) + max(min(change_ratio, 8.0), -8.0) + max(min(lead_change / 2.0, 5.0), -5.0)
    return round(max(0.0, min(88.0, score)), 2)


def _sector_candidate_metrics(
    source_name: str,
    board: Dict[str, Any],
    member: Dict[str, Any],
    board_rank: int,
    member_rank: int,
) -> Dict[str, Any]:
    metrics = {
        "source_name": source_name,
        "board_rank": board_rank,
        "member_rank": member_rank,
        "board_code": board.get("ts_code"),
        "board_name": board.get("name") or board.get("industry"),
        "board_change_ratio": board.get("change_ratio"),
        "board_net_inflow": board.get("net_inflow"),
        "lead_stock": board.get("lead_stock"),
        "lead_stock_pct_change": board.get("lead_stock_pct_change"),
        "company_num": board.get("company_num"),
        "member_weight": member.get("weight"),
        "member_in_date": member.get("in_date"),
        "member_is_new": member.get("is_new"),
    }
    return {key: value for key, value in metrics.items() if value not in (None, "", [], {})}


def _sector_reason_dimensions(source_name: str, board: Dict[str, Any], member: Dict[str, Any]) -> List[Dict[str, str]]:
    board_name = str(board.get("name") or board.get("industry") or board.get("ts_code") or "").strip()
    parts = []
    for key, label in (
        ("change_ratio", "板块涨跌幅"),
        ("net_inflow", "板块净流入"),
        ("lead_stock", "领涨股"),
        ("lead_stock_pct_change", "领涨股涨跌幅"),
        ("company_num", "成分数"),
    ):
        value = board.get(key)
        if value not in (None, ""):
            parts.append(f"{label}={_short_metric(value)}")
    member_weight = member.get("weight")
    if member_weight not in (None, ""):
        parts.append(f"成分权重={_short_metric(member_weight)}")
    label = "THS行业资金流" if source_name == "tushare_moneyflow_ind_ths" else "THS概念资金流"
    detail = f"{label}主题「{board_name}」：" + "；".join(parts[:5])
    return [{"dimension": "sentiment", "label": "板块主题", "detail": detail}]


def _capital_candidates_from_items(source_name: str, items: List[Dict[str, Any]], *, limit: int) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for rank, item in enumerate(items[: max(1, limit * 2)], start=1):
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or item.get("stock_code") or "").strip()
        if not (code.isdigit() and len(code) == 6):
            continue
        payload = {
            "code": code,
            "name": str(item.get("name") or item.get("stock_name") or code).strip(),
            "source": f"capital_flow:{source_name}",
            "reason": _capital_candidate_reason(source_name),
            "signal_score": _capital_candidate_score(source_name, item, rank),
            "strategy_tags": _capital_candidate_tags(source_name),
            "metrics": _capital_candidate_metrics(source_name, item, rank),
            "raw_source_item": item,
        }
        payload["reason_dimensions"] = _capital_reason_dimensions(payload)
        candidates.append(payload)
    return candidates

def _capital_candidate_reason(source_name: str) -> str:
    mapping = {
        "tushare_moneyflow_ths": "同花顺资金流主力净流入靠前候选，需后续验证价格结构和持续性。",
        "tushare_moneyflow_dc": "东财资金流主力净流入靠前候选，需后续验证价格结构和持续性。",
        "tushare_dragon_tiger_list": "龙虎榜上榜候选，需后续验证买卖额、封单与持续性。",
        "tushare_dragon_tiger_inst": "龙虎榜席位活跃候选，需后续验证机构/游资共振。",
        "tushare_limit_list_ths": "同花顺涨停榜候选，需后续验证连板、开板和封单强度。",
        "tushare_limit_list_d": "日涨停榜候选，需后续验证开板、封单和板块持续性。",
        "tushare_limit_step": "连板天梯候选，需后续验证连板高度和次日承接。",
        "tushare_hot_rank": "热榜候选，需后续验证热度是否能转化为真实资金承接。",
        "limit_up_pool": "涨停池资金活跃候选，需后续验证封单、开板次数和承接质量。",
        "popularity_rank": "市场人气排名靠前候选，需后续验证是否有资金承接。",
        "hot_money_activity": "游资/龙虎榜活跃候选，需后续验证净买入和持续性。",
    }
    return mapping.get(source_name, "资金活跃度候选，需后续验证。")


def _capital_candidate_tags(source_name: str) -> List[str]:
    mapping = {
        "tushare_moneyflow_ths": ["capital_flow", "tushare_moneyflow_ths", "main_inflow"],
        "tushare_moneyflow_dc": ["capital_flow", "tushare_moneyflow_dc", "main_inflow"],
        "tushare_dragon_tiger_list": ["capital_flow", "dragon_tiger", "market_activity"],
        "tushare_dragon_tiger_inst": ["capital_flow", "dragon_tiger", "seat_activity"],
        "tushare_limit_list_ths": ["capital_flow", "limit_up", "hot_money"],
        "tushare_limit_list_d": ["capital_flow", "limit_up", "hot_money"],
        "tushare_limit_step": ["capital_flow", "limit_up", "streak"],
        "tushare_hot_rank": ["capital_flow", "hot_rank", "attention"],
        "limit_up_pool": ["capital_flow", "limit_up", "hot_money"],
        "popularity_rank": ["capital_flow", "popularity", "attention"],
        "hot_money_activity": ["capital_flow", "hot_money", "dragon_tiger"],
    }
    return mapping.get(source_name, ["capital_flow"])


def _capital_candidate_score(source_name: str, item: Dict[str, Any], rank: int) -> float:
    rank_bonus = max(0.0, 12.0 - min(rank, 12) * 0.8)
    if source_name == "limit_up_pool":
        streak = _safe_float(item.get("limit_up_streak")) or 0.0
        turnover = _safe_float(item.get("turnover_ratio")) or 0.0
        ceiling_amount = _safe_float(item.get("ceiling_amount")) or 0.0
        bomb_num = _safe_float(item.get("bomb_num")) or 0.0
        score = 62.0 + rank_bonus + min(streak * 4.0, 12.0) + min(turnover, 8.0) + min(ceiling_amount / 100_000_000 * 2.0, 6.0) - min(bomb_num * 2.0, 10.0)
    elif source_name == "tushare_moneyflow_ths":
        net_inflow = _safe_float(item.get("net_inflow")) or 0.0
        net_5d_inflow = _safe_float(item.get("net_5d_inflow")) or 0.0
        change_ratio = _safe_float(item.get("change_ratio")) or 0.0
        score = 60.0 + rank_bonus + max(min(net_inflow / 100_000_000 * 5.0, 14.0), -14.0) + max(min(net_5d_inflow / 300_000_000 * 4.0, 10.0), -10.0) + max(min(change_ratio, 6.0), -6.0)
    elif source_name == "tushare_moneyflow_dc":
        net_inflow = _safe_float(item.get("net_inflow")) or 0.0
        net_inflow_rate = _safe_float(item.get("net_inflow_rate")) or 0.0
        change_ratio = _safe_float(item.get("change_ratio")) or 0.0
        score = 59.0 + rank_bonus + max(min(net_inflow / 100_000_000 * 5.0, 14.0), -14.0) + max(min(net_inflow_rate / 2.0, 8.0), -8.0) + max(min(change_ratio, 6.0), -6.0)
    elif source_name == "tushare_dragon_tiger_list":
        net_inflow = _safe_float(item.get("net_inflow")) or 0.0
        turnover_rate = _safe_float(item.get("turnover_rate")) or 0.0
        score = 60.0 + rank_bonus + max(min(net_inflow / 100_000_000 * 4.0, 12.0), -12.0) + max(min(turnover_rate, 8.0), 0.0)
    elif source_name == "tushare_dragon_tiger_inst":
        net_inflow = _safe_float(item.get("net_inflow")) or 0.0
        institution_seat_count = _safe_float(item.get("institution_seat_count")) or 0.0
        score = 58.0 + rank_bonus + max(min(net_inflow / 100_000_000 * 4.0, 12.0), -12.0) + min(institution_seat_count * 2.0, 10.0)
    elif source_name == "tushare_limit_list_ths":
        streak = _safe_float(item.get("limit_up_streak")) or 0.0
        ceiling_amount = _safe_float(item.get("ceiling_amount")) or 0.0
        turnover_rate = _safe_float(item.get("turnover_rate")) or 0.0
        score = 63.0 + rank_bonus + min(streak * 6.0, 18.0) + min(ceiling_amount / 100_000_000 * 2.0, 8.0) + min(turnover_rate, 8.0)
    elif source_name == "tushare_limit_list_d":
        streak = _safe_float(item.get("limit_up_streak")) or 0.0
        ceiling_amount = _safe_float(item.get("ceiling_amount")) or 0.0
        open_times = _safe_float(item.get("open_times")) or 0.0
        score = 63.0 + rank_bonus + min(streak * 6.0, 18.0) + min(ceiling_amount / 100_000_000 * 2.0, 8.0) - min(open_times * 2.0, 10.0)
    elif source_name == "tushare_limit_step":
        streak = _safe_float(item.get("limit_up_streak")) or 0.0
        score = 61.0 + rank_bonus + min(streak * 7.0, 18.0)
    elif source_name == "tushare_hot_rank":
        popularity = _safe_float(item.get("popularity")) or 0.0
        hot = _safe_float(item.get("hot")) or 0.0
        change_ratio = _safe_float(item.get("change_ratio")) or 0.0
        score = 57.0 + rank_bonus + min(popularity / 5.0, 12.0) + max(min(hot / 10_000_000, 8.0), 0.0) + max(min(change_ratio, 8.0), -8.0)
    elif source_name == "popularity_rank":
        popularity = _safe_float(item.get("popularity")) or 0.0
        change_ratio = _safe_float(item.get("change_ratio")) or 0.0
        score = 58.0 + rank_bonus + min(popularity / 500_000, 10.0) + max(min(change_ratio, 8.0), -8.0)
    elif source_name == "hot_money_activity":
        net_inflow = _safe_float(item.get("net_inflow")) or 0.0
        score = 56.0 + rank_bonus + max(min(net_inflow / 10_000_000, 12.0), -12.0)
    else:
        score = 55.0 + rank_bonus
    return round(max(0.0, min(92.0, score)), 2)


def _capital_candidate_metrics(source_name: str, item: Dict[str, Any], rank: int) -> Dict[str, Any]:
    keys = (
        "rank",
        "popularity",
        "change_ratio",
        "amount",
        "turnover_ratio",
        "ceiling_amount",
        "bomb_num",
        "limit_up_streak",
        "net_inflow",
        "net_5d_inflow",
        "large_net_inflow",
        "large_net_inflow_rate",
        "medium_net_inflow",
        "medium_net_inflow_rate",
        "small_net_inflow",
        "small_net_inflow_rate",
        "net_inflow_rate",
        "extra_large_net_inflow",
        "extra_large_net_inflow_rate",
        "seat_count",
        "institution_seat_count",
        "limit_up_streak",
        "limit_type",
        "status_label",
        "limit_order",
        "ceiling_amount",
        "open_times",
        "bomb_num",
        "up_stat",
        "first_limit_time",
        "last_limit_time",
        "data_type",
        "rank_time",
        "hot",
        "concepts",
        "limit_status",
        "buy_amount",
        "sell_amount",
        "industry",
        "concepts",
        "reason",
        "tag",
        "time",
        "date",
    )
    metrics = {key: item.get(key) for key in keys if item.get(key) not in (None, "", [], {})}
    metrics["source_name"] = source_name
    metrics["source_rank"] = rank
    return metrics


def _capital_reason_dimensions(item: Dict[str, Any]) -> List[Dict[str, str]]:
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    source_name = str(metrics.get("source_name") or "")
    bits: List[str] = []
    for key, label in (
        ("limit_up_streak", "连板数"),
        ("ceiling_amount", "封单额"),
        ("bomb_num", "开板次数"),
        ("turnover_ratio", "换手率"),
        ("amount", "成交额"),
        ("popularity", "人气值"),
        ("change_ratio", "涨跌幅"),
        ("net_inflow", "净买入"),
        ("net_5d_inflow", "5日净流入"),
        ("large_net_inflow", "大单净流入"),
        ("net_inflow_rate", "净流入率"),
        ("extra_large_net_inflow", "超大单净流入"),
        ("seat_count", "席位数"),
        ("institution_seat_count", "机构席位数"),
        ("limit_up_streak", "连板数"),
        ("ceiling_amount", "封单额"),
        ("open_times", "开板次数"),
        ("hot", "热度"),
    ):
        value = metrics.get(key)
        if value is not None:
            bits.append(f"{label}={_short_metric(value)}")
    label = {
        "tushare_moneyflow_ths": "THS资金流",
        "tushare_moneyflow_dc": "东财资金流",
        "tushare_dragon_tiger_list": "龙虎榜",
        "tushare_dragon_tiger_inst": "龙虎榜席位",
        "tushare_limit_list_ths": "THS涨停榜",
        "tushare_limit_list_d": "涨停榜",
        "tushare_limit_step": "连板天梯",
        "tushare_hot_rank": "热榜",
        "limit_up_pool": "涨停池",
        "popularity_rank": "人气榜",
        "hot_money_activity": "游资/龙虎榜",
    }.get(source_name, "资金活跃")
    return [{"dimension": "capital", "label": "资金面", "detail": f"{label}候选：" + "；".join(bits[:5])}]


def _merge_raw_capital_candidates(candidates: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    by_code: Dict[str, Dict[str, Any]] = {}
    for item in candidates:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        if code not in by_code:
            by_code[code] = dict(item)
            continue
        current = by_code[code]
        current["signal_score"] = round(max(float(current.get("signal_score") or 0), float(item.get("signal_score") or 0)) + 4.0, 2)
        current["source"] = "capital_flow:multi_source"
        current["reason"] = "多个资金活跃度来源共振。"
        current_sources = list(current.get("recall_sources") or [current.get("source")])
        for source in [item.get("source")]:
            if source and source not in current_sources:
                current_sources.append(source)
        current["recall_sources"] = current_sources
        metrics = dict(current.get("metrics") or {})
        metrics[str(item.get("source") or "capital_flow")] = item.get("metrics") or {}
        current["metrics"] = metrics
        current["reason_dimensions"] = _capital_reason_dimensions(current)
    merged = list(by_code.values())
    merged.sort(key=lambda item: (float(item.get("signal_score") or 0), str(item.get("code") or "")), reverse=True)
    return merged[: max(1, int(limit or 1))]


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        parsed = float(str(value).replace(",", "").replace("%", "").strip())
        return parsed if math.isfinite(parsed) else None
    except Exception:
        return None


def _sanitize_json_like(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _sanitize_json_like(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_like(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_json_like(item) for item in value]
    return value


def _short_metric(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return str(value)
    if abs(number) >= 100_000_000:
        return f"{number / 100_000_000:.2f}亿"
    if abs(number) >= 10_000:
        return f"{number / 10_000:.2f}万"
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _default_soft_quotas() -> Dict[str, Dict[str, int]]:
    return {
        "strategy_factor_expert": {"min": 2, "max": 4},
        "technical_candidate_expert": {"min": 2, "max": 4},
        "capital_flow_expert": {"min": 1, "max": 3},
        "fundamental_expert": {"min": 1, "max": 3},
        "sector_theme_expert": {"min": 0, "max": 2},
        "news_event_expert": {"min": 0, "max": 2},
        "sentiment_theme_expert": {"min": 0, "max": 1},
        "fallback_seed_pool": {"min": 0, "max": 0},
    }


def _candidate_quality_summary(
    candidates: List[Dict[str, Any]],
    packets: List[ExpertCandidatePacket],
    hard_exclusion: Dict[str, Any],
) -> Dict[str, Any]:
    dimensions: Dict[str, int] = {}
    experts: Dict[str, int] = {}
    lifecycle: Dict[str, int] = {}
    fallback_count = 0
    for item in candidates:
        if item.get("source") == "fallback_seed_pool" or "fallback_seed_pool" in (item.get("recall_sources") or []):
            fallback_count += 1
        lifecycle_status = str(item.get("lifecycle_status") or "new")
        lifecycle[lifecycle_status] = lifecycle.get(lifecycle_status, 0) + 1
        for dimension in item.get("candidate_dimensions") or []:
            key = str(dimension or "unknown")
            dimensions[key] = dimensions.get(key, 0) + 1
        for expert in item.get("candidate_experts") or []:
            key = str(expert or "unknown")
            experts[key] = experts.get(key, 0) + 1
    packet_status = {packet.expert: packet.status for packet in packets}
    strategy_count = experts.get("strategy_factor_expert", 0)
    technical_count = experts.get("technical_candidate_expert", 0)
    return {
        "candidate_count": len(candidates),
        "dimension_counts": dimensions,
        "expert_counts": experts,
        "lifecycle_counts": lifecycle,
        "fallback_count": fallback_count,
        "hard_strategy_trunk_missing": strategy_count == 0 and technical_count == 0,
        "hard_exclusion_count": int(hard_exclusion.get("excluded_count") or 0),
        "packet_status": packet_status,
    }
