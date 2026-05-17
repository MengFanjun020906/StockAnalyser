# -*- coding: utf-8 -*-
"""First-stage multi-expert candidate discovery orchestration."""

from __future__ import annotations

import concurrent.futures
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
            ),
            "capital_flow_expert": lambda: self._empty_packet(
                expert="capital_flow_expert",
                dimension="capital",
                reason="Capital-flow full-market discovery is not wired yet; evaluate phase still validates capital flow per candidate.",
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

    def _fundamental_packet(self, *, limit: int, strategy_names: Optional[Sequence[str]]) -> ExpertCandidatePacket:
        result = FundamentalCandidateProvider().discover(limit=limit, strategy_names=strategy_names)
        candidates = [
            _candidate_from_raw(
                item,
                expert="fundamental_expert",
                dimension="fundamental",
                default_reason="基本面质量/成长/估值预计算候选。",
                evidence_prefix="fundamental",
                default_confidence=0.58,
            )
            for item in result.get("candidates") or []
            if item.get("code")
        ]
        return ExpertCandidatePacket(
            expert="fundamental_expert",
            dimension="fundamental",
            status=_status_from_provider(result),
            data_quality=CandidateDataQuality(
                freshness="filing_or_weekly",
                as_of=result.get("latest_period") or result.get("updated_at"),
                source_chain=[{"provider": "fundamental_candidate_snapshot", "db_path": result.get("db_path"), "table": result.get("table")}],
                warnings=[] if candidates else [str(result.get("error") or "Fundamental candidate snapshot produced no candidates")],
            ),
            candidates=candidates,
            diagnostics=result.get("diagnostics") or [],
            errors=[str(result.get("error"))] if result.get("error") else [],
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
        if not sectors:
            sectors = top_sector_names(5)
            diagnostics.append({"source": "get_sector_rankings", "status": "ok" if sectors else "empty", "sectors": sectors})
        raw_candidates: List[Dict[str, Any]] = []
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
                source_chain=[{"provider": "akshare/eastmoney_sector_constituents", "sectors": sectors}],
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
        candidates = [
            _candidate_from_raw(
                item,
                expert="news_event_expert",
                dimension="news_event",
                default_reason="公司级新闻/公告事件候选。",
                evidence_prefix="news_momentum",
                default_confidence=0.56,
            )
            for item in result.get("candidates") or []
            if item.get("code")
        ]
        return ExpertCandidatePacket(
            expert="news_event_expert",
            dimension="news_event",
            status=_status_from_provider(result),
            data_quality=CandidateDataQuality(
                freshness="recent",
                source_chain=[{"provider": "search/news_momentum"}],
                warnings=[] if candidates else ["News momentum produced no stock candidates"],
            ),
            candidates=candidates,
            diagnostics=[*(result.get("diagnostics") or []), *(_as_dict_list(result.get("queries")))],
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
