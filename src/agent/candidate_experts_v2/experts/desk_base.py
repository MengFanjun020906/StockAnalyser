# -*- coding: utf-8 -*-
"""BaseDeskExpert — extends BaseExpert to accept List[FeatureRow] as input.

Key changes vs BaseExpert:
1. `run(rows, market, regime)` replaces `run(seed_pool, market)`.
2. `_build_user_message` renders FactSheet + FeatureFlags per stock.
3. `_filter_eligible_rows` hook lets subclasses apply cheap eligibility
   filtering (cost control), with mandatory OR fallback so the desk never
   silently starves on missing data.
4. max_llm_rounds=5 / max_tool_calls=10 desk defaults (per spec §3.1).
"""

from __future__ import annotations

import copy
import concurrent.futures
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.agent.candidate_experts_v2.cache import cache_key, save_packet
from src.agent.candidate_experts_v2.experts.base import BaseExpert, LLMCallable
from src.agent.candidate_experts_v2.seed_facts import compact_seed_fact_packets_for_model
from src.agent.candidate_experts_v2.schemas import (
    ExpertDataQualityV2,
    ExpertPacketV2,
    FactSheet,
    FeatureRow,
    SeedItem,
    SeedSummaryV2,
)

logger = logging.getLogger(__name__)

_FALLBACK_SUPPLEMENT_N = 10  # used when subclass has no config access


class BaseDeskExpert(BaseExpert):
    """Thesis desk expert.  Subclasses define their eligibility filter."""

    expert_name: str = "base_desk_expert"
    dimension: str = "desk"

    def __init__(
        self,
        *,
        allowed_tools: Sequence[str],
        tool_registry: Dict[str, Any],
        tool_decls: Sequence[Dict[str, Any]],
        llm: LLMCallable,
        system_prompt: str,
        max_llm_rounds: int = 5,
        max_tool_calls: int = 10,
        freshness: str = "intraday",
        fallback_supplement_n: int = _FALLBACK_SUPPLEMENT_N,
    ) -> None:
        super().__init__(
            allowed_tools=allowed_tools,
            tool_registry=tool_registry,
            tool_decls=tool_decls,
            llm=llm,
            system_prompt=system_prompt,
            max_llm_rounds=max_llm_rounds,
            max_tool_calls=max_tool_calls,
            freshness=freshness,
        )
        self._fallback_supplement_n = fallback_supplement_n
        # thread-safe: each desk instance is used by one caller at a time
        self._desk_rows: List[FeatureRow] = []
        self._desk_regime: str = "unknown"

    # ------------------------------------------------------------------
    # Public entry point (replaces BaseExpert.run)
    # ------------------------------------------------------------------

    def run_desk(
        self,
        rows: List[FeatureRow],
        *,
        market: str = "cn",
        regime: str = "unknown",
        use_cache: bool = False,
        deadline_s: Optional[float] = None,
        per_seed_timeout_s: Optional[float] = None,
        max_consecutive_seed_timeouts: int = 2,
    ) -> ExpertPacketV2:
        """Run this desk on a list of FeatureRows.

        Eligibility filtering is applied first (subclass hook), then the
        bounded LLM loop is invoked via BaseExpert._run_uncached — which
        calls self._build_user_message, overridden here to use FeatureRows.

        Cache is off by default for desk mode (regime changes daily).
        """
        started = time.time()

        eligible = self._filter_eligible_rows(rows)
        if not eligible:
            logger.debug("%s: no eligible rows after filtering", self.expert_name)
            packet = ExpertPacketV2(
                expert=self.expert_name,
                dimension=self.dimension,
                status="empty",
                seed_summary=SeedSummaryV2(seed_count=0),
                candidates=[],
                rejected=[],
                diagnostics=[{"source": "desk_filter", "status": "no_eligible_rows"}],
            )
            packet.elapsed_ms = int((time.time() - started) * 1000)
            return packet

        row_packets: List[ExpertPacketV2] = []
        consecutive_failures = 0
        consecutive_failure_statuses: List[str] = []
        seed_timeout_s = float(per_seed_timeout_s or 180.0)
        stop_reason = ""

        for idx, row in enumerate(eligible):
            if deadline_s is not None:
                remaining = deadline_s - time.time()
                if remaining <= 1.0:
                    stop_reason = "desk_deadline_exhausted"
                    row_packets.extend(
                        self._skipped_row_packet(
                            skipped,
                            reason=stop_reason,
                            market=market,
                            regime=regime,
                        )
                        for skipped in eligible[idx:]
                    )
                    break
                effective_timeout = max(1.0, min(seed_timeout_s, remaining - 0.5))
            else:
                effective_timeout = seed_timeout_s

            packet = self._run_one_row_with_timeout(
                row,
                market=market,
                regime=regime,
                timeout_s=effective_timeout,
            )
            row_packets.append(packet)
            if packet.status in {"failed", "timeout"}:
                consecutive_failures += 1
                consecutive_failure_statuses.append(str(packet.status))
            else:
                consecutive_failures = 0
                consecutive_failure_statuses = []
            if consecutive_failures >= max(1, int(max_consecutive_seed_timeouts)):
                stop_reason = (
                    "consecutive_seed_timeouts"
                    if set(consecutive_failure_statuses) == {"timeout"}
                    else "consecutive_seed_failures"
                )
                row_packets.extend(
                    self._skipped_row_packet(
                        skipped,
                        reason=stop_reason,
                        market=market,
                        regime=regime,
                    )
                    for skipped in eligible[idx + 1 :]
                )
                break

        return self._merge_row_packets(
            eligible,
            row_packets,
            elapsed_ms=int((time.time() - started) * 1000),
            stop_reason=stop_reason,
        )

    def _run_one_row_with_timeout(
        self,
        row: FeatureRow,
        *,
        market: str,
        regime: str,
        timeout_s: float,
    ) -> ExpertPacketV2:
        """Run one row with a hard wall-clock guard for trace-visible partials."""

        started = time.time()
        worker = copy.copy(self)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(worker._run_one_row, row, market=market, regime=regime)
        try:
            packet = future.result(timeout=max(1.0, timeout_s))
            executor.shutdown(wait=False, cancel_futures=True)
            return packet
        except concurrent.futures.TimeoutError:
            executor.shutdown(wait=False, cancel_futures=True)
            packet = self._timeout_row_packet(
                row,
                timeout_s=timeout_s,
                elapsed_ms=int((time.time() - started) * 1000),
            )
            self._persist_single_row_packet(packet, row=row, market=market, regime=regime)
            return packet
        except Exception:
            executor.shutdown(wait=False, cancel_futures=True)
            raise

    def _timeout_row_packet(
        self,
        row: FeatureRow,
        *,
        timeout_s: float,
        elapsed_ms: int,
    ) -> ExpertPacketV2:
        seed = _seed_from_row(row)
        return ExpertPacketV2(
            expert=self.expert_name,
            dimension=self.dimension,
            status="timeout",
            seed_summary=SeedSummaryV2(seed_count=1, seed_sources={seed.source: 1}),
            data_quality=ExpertDataQualityV2(
                freshness=self.freshness,
                warnings=["single seed timed out before producing a packet"],
            ),
            diagnostics=[
                {
                    "source": "desk_single_seed_timeout",
                    "status": "timeout",
                    "code": row.code,
                    "timeout_s": round(float(timeout_s), 3),
                }
            ],
            errors=[f"{self.expert_name} seed {row.code} timeout after {timeout_s:.1f}s"],
            elapsed_ms=max(0, int(elapsed_ms)),
        )

    def _skipped_row_packet(
        self,
        row: FeatureRow,
        *,
        reason: str,
        market: str,
        regime: str,
    ) -> ExpertPacketV2:
        seed = _seed_from_row(row)
        packet = ExpertPacketV2(
            expert=self.expert_name,
            dimension=self.dimension,
            status="unavailable",
            seed_summary=SeedSummaryV2(seed_count=1, seed_sources={seed.source: 1}),
            data_quality=ExpertDataQualityV2(
                freshness=self.freshness,
                warnings=[f"single seed skipped: {reason}"],
            ),
            diagnostics=[
                {
                    "source": "desk_single_seed_skipped",
                    "status": "skipped",
                    "code": row.code,
                    "reason": reason,
                }
            ],
            errors=[f"{self.expert_name} seed {row.code} skipped: {reason}"],
        )
        self._persist_single_row_packet(packet, row=row, market=market, regime=regime)
        return packet

    def _run_one_row(
        self,
        row: FeatureRow,
        *,
        market: str,
        regime: str,
    ) -> ExpertPacketV2:
        """Run one LLM decision against exactly one FeatureRow and persist it."""

        started = time.time()
        dummy_seed = _seed_from_row(row)
        self._desk_rows = [row]
        self._desk_regime = regime
        try:
            packet = self._run_uncached([dummy_seed], market=market)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("%s single-row run failed for %s: %s", self.expert_name, row.code, exc)
            packet = ExpertPacketV2(
                expert=self.expert_name,
                dimension=self.dimension,
                status="failed",
                seed_summary=SeedSummaryV2(seed_count=1, seed_sources={dummy_seed.source: 1}),
                data_quality=ExpertDataQualityV2(
                    freshness=self.freshness,
                    warnings=[f"single-row run raised {type(exc).__name__}"],
                ),
                errors=[str(exc)],
            )
        finally:
            self._desk_rows = []
            self._desk_regime = "unknown"

        packet.elapsed_ms = int((time.time() - started) * 1000)
        self._enforce_single_row_scope(packet, row)
        self._persist_single_row_packet(packet, row=row, market=market, regime=regime)
        return packet

    def _enforce_single_row_scope(self, packet: ExpertPacketV2, row: FeatureRow) -> None:
        """Drop hallucinated codes from a one-row prompt."""

        expected = str(row.code)
        kept = []
        dropped = []
        for candidate in packet.candidates or []:
            if str(candidate.code) == expected:
                if not candidate.name:
                    candidate.name = row.name or expected
                kept.append(candidate)
            else:
                dropped.append(str(candidate.code))
        if dropped:
            packet.diagnostics.append(
                {
                    "source": "desk_single_seed_scope",
                    "status": "dropped_out_of_scope_candidates",
                    "expected_code": expected,
                    "dropped_codes": dropped,
                }
            )
        packet.candidates = kept
        if kept and packet.status in {"empty", "failed"}:
            packet.status = "partial" if packet.errors else "ok"
        if not kept and packet.status == "ok":
            packet.status = "empty"

    def _persist_single_row_packet(
        self,
        packet: ExpertPacketV2,
        *,
        row: FeatureRow,
        market: str,
        regime: str,
    ) -> None:
        row_hash = _row_packet_hash(row, regime=regime)
        key = cache_key(f"{self.expert_name}:single", market, row_hash)
        packet.diagnostics.append(
            {
                "source": "desk_single_seed_checkpoint",
                "status": "saved",
                "code": row.code,
                "cache_key": key,
            }
        )
        try:
            save_packet(key, packet, dimension=self.dimension)
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("single-row packet save failed for %s %s: %s", self.expert_name, row.code, exc)

    def _merge_row_packets(
        self,
        rows: Sequence[FeatureRow],
        packets: Sequence[ExpertPacketV2],
        *,
        elapsed_ms: int,
        stop_reason: str = "",
    ) -> ExpertPacketV2:
        candidates = []
        rejected: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        diagnostics: List[Dict[str, Any]] = [
            {
                "source": "desk_single_seed_loop",
                "status": "completed",
                "seed_count": len(rows),
                "packet_count": len(packets),
                "stop_reason": stop_reason,
            }
        ]
        errors: List[str] = []
        source_counts: Dict[str, int] = {}
        source_chain: List[str] = []
        warnings: List[str] = []

        for row, packet in zip(rows, packets):
            source = _seed_source_from_row(row)
            source_counts[source] = source_counts.get(source, 0) + 1
            candidates.extend(packet.candidates or [])
            rejected.extend(packet.rejected or [])
            for call in packet.tool_calls or []:
                enriched = dict(call)
                enriched.setdefault("stock_code", row.code)
                tool_calls.append(enriched)
            diagnostics.append(
                {
                    "source": "desk_single_seed_packet",
                    "code": row.code,
                    "status": packet.status,
                    "candidate_count": len(packet.candidates or []),
                    "rejected_count": len(packet.rejected or []),
                    "tool_call_count": len(packet.tool_calls or []),
                    "elapsed_ms": packet.elapsed_ms,
                    "cache_hit": packet.cache_hit,
                }
            )
            diagnostics.extend(packet.diagnostics or [])
            errors.extend([f"{row.code}: {err}" for err in (packet.errors or []) if err])
            dq = packet.data_quality
            source_chain.extend([str(item) for item in (dq.source_chain or []) if item])
            warnings.extend([str(item) for item in (dq.warnings or []) if item])

        packet_statuses = [str(packet.status) for packet in packets]
        if candidates:
            status = "ok"
        elif any(s in {"failed", "timeout", "unavailable"} for s in packet_statuses):
            status = "failed"
        elif packets:
            status = "empty"
        else:
            status = "empty"

        return ExpertPacketV2(
            expert=self.expert_name,
            dimension=self.dimension,
            status=status,
            seed_summary=SeedSummaryV2(
                seed_count=len(rows),
                accepted_count=len(candidates),
                rejected_count=len(rejected),
                seed_sources=source_counts,
            ),
            data_quality=ExpertDataQualityV2(
                freshness=self.freshness,
                source_chain=list(dict.fromkeys(source_chain)),
                warnings=list(dict.fromkeys(warnings)),
            ),
            candidates=candidates,
            rejected=rejected,
            tool_calls=tool_calls,
            diagnostics=diagnostics,
            errors=errors,
            elapsed_ms=elapsed_ms,
            per_seed_packets=[packet.model_dump(mode="json") for packet in packets],
        )

    # ------------------------------------------------------------------
    # Hook: subclasses implement eligibility filtering
    # ------------------------------------------------------------------

    def _filter_eligible_rows(self, rows: List[FeatureRow]) -> List[FeatureRow]:
        """Return the subset of rows this desk should evaluate.

        Default: return all rows (no filtering).  Subclasses override to
        apply cheap eligibility criteria with mandatory OR fallback.
        """
        return rows

    # ------------------------------------------------------------------
    # Override: build user message from FeatureRows + FactSheets
    # ------------------------------------------------------------------

    def _build_user_message(
        self,
        seeds: Sequence[SeedItem],
        *,
        market: str,
    ) -> str:
        rows = self._desk_rows
        regime = self._desk_regime

        candidates_payload = []
        for row in rows[:30]:
            fs = row.fact_sheet
            fs_dict = _fact_sheet_to_dict(fs) if fs else {}
            seed_fact_packets = compact_seed_fact_packets_for_model(
                [row.seed_fact],
                limit=1,
            ) if getattr(row, "seed_fact", None) is not None else []
            seed_fact_dict = seed_fact_packets[0] if seed_fact_packets else {}
            candidates_payload.append(
                {
                    "code": row.code,
                    "name": row.name,
                    "market": row.market,
                    "recall_sources": row.recall_sources,
                    "flags": [
                        {
                            "detector": f.detector,
                            "kind": f.kind,
                            "summary": f.summary,
                            "metrics": f.metrics,
                        }
                        for f in row.flags
                    ],
                    "fact_sheet": fs_dict,
                    "seed_fact": seed_fact_dict,
                }
            )

        return (
            f"市场: {market}\n"
            f"市场状态(regime): {regime}\n"
            f"席位: {self.expert_name}\n"
            f"候选池（最多 30 只，每只附带 FactSheet + SeedFactPacket + FeatureFlags）:\n"
            f"{json.dumps(candidates_payload, ensure_ascii=False)}\n\n"
            "请优先读取 seed_fact.facts 里已经预取的工具结果；只有缺失、失败、冲突或本席位关键二次确认时才补充调用工具。"
            "按 system prompt 的要求给出证据，并以 JSON 输出最终候选。"
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _fact_sheet_to_dict(fs: FactSheet) -> Dict[str, Any]:
    """Serialize FactSheet fields relevant to desk decision-making."""
    return {
        "capital_direction": fs.capital_direction,
        "capital_violent_outflow": fs.capital_violent_outflow,
        "trend_state": fs.trend_state,
        "breakdown_accelerating": fs.breakdown_accelerating,
        "range_pct_60": fs.range_pct_60,
        "range_pct_120": fs.range_pct_120,
        "dist_to_high_20": fs.dist_to_high_20,
        "gain_5d": fs.gain_5d,
        "bias_ma20": fs.bias_ma20,
        "volume_ratio": fs.volume_ratio,
        "rsi14": fs.rsi14,
        "liquidity_ok": fs.liquidity_ok,
        "hard_risk_flags": fs.hard_risk_flags,
        "sector_name": fs.sector_name,
        "sector_strength": fs.sector_strength,
        "leader_already_up": fs.leader_already_up,
    }


def _seed_source_from_row(row: FeatureRow) -> str:
    for source in row.recall_sources or []:
        source_text = str(source or "").strip()
        if source_text:
            return source_text
    return "fallback"


def _seed_from_row(row: FeatureRow) -> SeedItem:
    source = _seed_source_from_row(row)
    kwargs: Dict[str, Any] = {
        "code": row.code,
        "name": row.name,
        "market": row.market,
        "hint": "; ".join(str(flag.summary) for flag in row.flags[:3] if flag.summary),
        "trigger_signals": [
            {
                "detector": flag.detector,
                "kind": flag.kind,
                "summary": flag.summary,
                "metrics": flag.metrics,
                "as_of": flag.as_of,
            }
            for flag in row.flags[:5]
        ],
    }
    try:
        return SeedItem(source=source, **kwargs)
    except Exception:
        return SeedItem(source="fallback", **kwargs)


def _row_packet_hash(row: FeatureRow, *, regime: str) -> str:
    compact_seed_fact = compact_seed_fact_packets_for_model(
        [row.seed_fact],
        limit=1,
    ) if getattr(row, "seed_fact", None) is not None else []
    payload = {
        "code": row.code,
        "market": row.market,
        "recall_sources": row.recall_sources,
        "flags": [flag.model_dump(mode="json") for flag in row.flags],
        "fact_sheet": row.fact_sheet.model_dump(mode="json") if row.fact_sheet else None,
        "seed_fact": compact_seed_fact[0] if compact_seed_fact else None,
        "regime": regime,
    }
    digest = hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"{row.code}:{digest[:12]}"
