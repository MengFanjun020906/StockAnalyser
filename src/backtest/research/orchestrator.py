# -*- coding: utf-8 -*-
"""P3 historical-Agent research orchestrator.

The orchestrator is called by the execution engine at each monthly decision
point.  It sees only the as-of market view and the simulated portfolio snapshot;
it returns machine-priced ReplayPlans for the same execution/ledger kernel.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from src.backtest.calendar import CnTradingCalendar
from src.backtest.data_view import InMemoryDataView
from src.backtest.schemas import ExperimentConfig, ReplayPlan, ResearchConfirmationMode
from src.backtest.snapshot import SnapshotBuildResult

from .agents import ConfirmationResult, run_confirmation, run_selection
from .llm import ResearchBudgetExceeded, ResearchCache, ResearchLLM
from .market import AsOfMarketView, build_research_plan

logger = logging.getLogger(__name__)


class ResearchOrchestrator:
    def __init__(
        self,
        *,
        config: ExperimentConfig,
        data: InMemoryDataView,
        calendar: CnTradingCalendar,
        cache_path: Path | None = None,
        llm_adapter: Any | None = None,
        universe: list[str] | None = None,
        universe_source: str = "explicit",
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        if config.research is None:
            raise ValueError("research config is required")
        self.config = config
        self.research = config.research
        self.universe = list(universe or self.research.universe)
        self.universe_source = universe_source
        self.data = data
        self.calendar = calendar
        self.market = AsOfMarketView(
            data,
            calendar,
            min_history_bars=self.research.min_history_bars,
        )
        self.cache = ResearchCache(cache_path)
        self.llm = ResearchLLM(
            max_calls=self.research.max_llm_calls,
            cache=self.cache,
            adapter=llm_adapter,
            model_ref=self.research.model_ref,
            timeout_seconds=self.research.llm_timeout_seconds,
        )
        self.progress_callback = progress_callback
        self.decision_dates = self._decision_dates()
        self.decisions: list[dict[str, Any]] = []
        self.skipped: list[dict[str, Any]] = []
        self._budget_exhausted = False

    # ------------------------------------------------------------------
    # Precheck
    # ------------------------------------------------------------------
    def research_precheck(self, snapshot: SnapshotBuildResult) -> tuple[list[str], list[str], dict[str, Any]]:
        blockers: list[str] = []
        warnings: list[str] = []
        labels: list[str] = ["survivor_bias", "model_knowledge_leakage_risk", "llm_nondeterminism"]
        if self.universe_source != "explicit":
            labels.append("local_coverage_universe")
        if self.research.confirmation_mode is ResearchConfirmationMode.AUTO_ACCEPT:
            labels.append("auto_accept_selection")
        tool_status = self.market.tool_status()
        tool_gaps = {key: value for key, value in tool_status.items() if value == "gap"}
        blocked_tools = {key: value for key, value in tool_status.items() if value == "blocked"}
        if tool_gaps:
            warnings.append("research_tool_gaps:" + ",".join(sorted(tool_gaps)))
            labels.append("tool_gap")
            if not self.research.allow_tool_gaps:
                blockers.append("research_tool_gaps_not_allowed")
        if not self.decision_dates:
            blockers.append("no_research_decision_dates")
        estimate = self.estimated_calls()
        if estimate > self.research.max_llm_calls:
            blockers.append("research_budget_exceeded")
        if not self.llm.is_available():
            blockers.append("research_llm_unavailable")
        if self.llm.requested_model_ref and self.llm.requested_model_ref != self.llm.model_ref:
            warnings.append("research_model_ref_not_applied")
        if not snapshot.minute_bars and not snapshot.daily_bars:
            blockers.append("no_research_market_data")
        valid_symbols = set()
        for decision in self.decision_dates:
            try:
                as_of = self.calendar.previous_session(decision)
            except Exception:
                warnings.append(f"research_previous_session_missing:{decision.isoformat()}")
                continue
            snapshots, gaps = self.market.universe_snapshots(self.universe, as_of)
            if snapshots:
                valid_symbols.update(snapshots)
        if not valid_symbols:
            blockers.append("insufficient_research_history")
        elif len(valid_symbols) < len(self.universe):
            warnings.append(f"partial_research_universe:{len(valid_symbols)}/{len(self.universe)}")
        coverage_rows = snapshot.coverage
        incomplete = [row for row in coverage_rows if row.sessions_missing or row.sessions_partial]
        if incomplete and not valid_symbols:
            blockers.append("incomplete_research_market_data")
        diagnostics = {
            "research": {
                "universe": self.universe,
                "universe_size": len(self.universe),
                "universe_mode": self.research.universe_mode.value,
                "universe_source": self.universe_source,
                "confirmation_mode": self.research.confirmation_mode.value,
                "decision_dates": [item.isoformat() for item in self.decision_dates],
                "decision_count": len(self.decision_dates),
                "frequency": self.research.decision_frequency.value,
                "selection_top_n": self.research.selection_top_n,
                "max_confirmations_per_decision": self.research.max_confirmations_per_decision,
                "estimated_llm_calls": estimate,
                "max_llm_calls": self.research.max_llm_calls,
                "model_ref": self.llm.model_ref,
                "requested_model_ref": self.llm.requested_model_ref,
                "prompt_version": self.research.prompt_version,
                "plan_version": self.research.plan_version,
                "tool_status": tool_status,
                "tool_gaps": tool_gaps,
                "blocked_tools": blocked_tools,
                "valid_symbols": sorted(valid_symbols),
                "labels": sorted(set(labels)),
            }
        }
        if valid_symbols:
            warnings.append("research_not_a_production_strategy")
        return sorted(set(blockers)), sorted(set(warnings)), diagnostics

    def estimated_calls(self) -> int:
        if self.research.confirmation_mode is ResearchConfirmationMode.AUTO_ACCEPT:
            return len(self.decision_dates)
        per_decision = 1 + min(
            self.research.selection_top_n,
            self.research.max_confirmations_per_decision,
        )
        return len(self.decision_dates) * per_decision

    # ------------------------------------------------------------------
    # Decision point
    # ------------------------------------------------------------------
    def plan_for_decision(self, session: date, portfolio: dict[str, Any]) -> list[ReplayPlan]:
        if session not in self.decision_dates:
            return []
        as_of = self.calendar.previous_session(session)
        self._emit("research_decision_start", {"session": session.isoformat(), "as_of": as_of.isoformat()})
        snapshots, gaps = self.market.universe_snapshots(self.universe, as_of)
        for symbol, reason in gaps.items():
            self.skipped.append({"session": session.isoformat(), "symbol": symbol, "reason": f"data_gap:{reason}"})
        if not snapshots:
            self.decisions.append(
                {
                    "session": session.isoformat(),
                    "as_of": as_of.isoformat(),
                    "status": "no_market_data",
                    "selected": [],
                    "confirmed": [],
                }
            )
            return []
        snapshots, one_lot_skipped = self._filter_one_lot_executable(
            snapshots,
            portfolio,
            session,
            as_of,
        )
        for item in one_lot_skipped:
            self.skipped.append({"session": session.isoformat(), **item})
        if not snapshots:
            self.decisions.append(
                {
                    "session": session.isoformat(),
                    "as_of": as_of.isoformat(),
                    "status": "no_executable_candidates",
                    "confirmation_mode": self.research.confirmation_mode.value,
                    "selected": [],
                    "confirmed": [],
                    "plans": [],
                    "skipped": len(one_lot_skipped),
                }
            )
            return []
        tool_status = self.market.tool_status()
        tool_gaps = {key: value for key, value in tool_status.items() if value == "gap"}
        try:
            selection = run_selection(
                llm=self.llm,
                snapshots=snapshots,
                as_of=as_of,
                portfolio=portfolio,
                config=self.config,
                tool_gaps=tool_gaps,
            )
        except ResearchBudgetExceeded:
            self._budget_exhausted = True
            self.skipped.append({"session": session.isoformat(), "reason": "research_budget_exceeded"})
            return []
        except Exception as exc:
            logger.exception("research selection failed: %s", exc)
            self.skipped.append({"session": session.isoformat(), "reason": f"selection_failed:{exc}"})
            return []

        plans: list[ReplayPlan] = []
        confirmed: list[dict[str, Any]] = []
        held = {
            str(item.get("symbol") or "")
            for item in portfolio.get("positions", [])
            if int(item.get("quantity") or 0) > 0
        }
        for item in selection.selected:
            symbol = str(item["symbol"])
            if symbol in held:
                self.skipped.append(
                    {"session": session.isoformat(), "symbol": symbol, "reason": "already_position"}
                )
                continue
            snapshot = snapshots.get(symbol)
            if snapshot is None:
                continue
            rule = self.market.rule(symbol, session)
            draft = build_research_plan(
                config=self.config,
                snapshot=snapshot,
                rule=rule,
                available_at=self.calendar.session_close(as_of),
                valid_from=session,
                rank=int(item.get("rank") or 1),
            )
            if draft is None:
                self.skipped.append(
                    {"session": session.isoformat(), "symbol": symbol, "reason": "research_plan_unavailable"}
                )
                continue
            if not self._can_afford(portfolio, draft.plan, rule.buy_lot_size):
                self.skipped.append(
                    {"session": session.isoformat(), "symbol": symbol, "reason": "insufficient_cash_or_exposure"}
                )
                continue
            if self.research.confirmation_mode is ResearchConfirmationMode.AUTO_ACCEPT:
                confirmation = ConfirmationResult(
                    accepted=True,
                    confidence=1.0,
                    reason="auto_accept_selection",
                )
            else:
                try:
                    confirmation = run_confirmation(
                        llm=self.llm,
                        draft=draft,
                        portfolio=portfolio,
                        config=self.config,
                    )
                except ResearchBudgetExceeded:
                    self._budget_exhausted = True
                    self.skipped.append(
                        {"session": session.isoformat(), "symbol": symbol, "reason": "research_budget_exceeded"}
                    )
                    break
                except Exception as exc:
                    logger.exception("research confirmation failed: %s", exc)
                    self.skipped.append(
                        {"session": session.isoformat(), "symbol": symbol, "reason": f"confirmation_failed:{exc}"}
                    )
                    continue
            confirmed.append(
                {
                    "symbol": symbol,
                    "decision": "accept" if confirmation.accepted else "reject",
                    "confidence": confirmation.confidence,
                    "reason": confirmation.reason,
                    "gap": confirmation.gap,
                    "mode": self.research.confirmation_mode.value,
                }
            )
            if confirmation.accepted:
                plans.append(draft.plan)
        self.decisions.append(
            {
                "session": session.isoformat(),
                "as_of": as_of.isoformat(),
                "status": "planned" if plans else "no_trade",
                "selection_fallback": selection.fallback,
                "selection_gap": selection.gap,
                "confirmation_mode": self.research.confirmation_mode.value,
                "selected": selection.selected,
                "no_trade_reason": selection.no_trade_reason,
                "confirmed": confirmed,
                "plans": [plan.plan_key for plan in plans],
                "tool_gaps": tool_gaps,
            }
        )
        self._emit(
            "research_decision_done",
            {
                "session": session.isoformat(),
                "selected": len(selection.selected),
                "plans": len(plans),
                "llm_calls": self.llm.llm_call_count,
            },
        )
        return plans

    def diagnostics(self) -> dict[str, Any]:
        tool_status = self.market.tool_status()
        labels = {
            "survivor_bias",
            "model_knowledge_leakage_risk",
            "llm_nondeterminism",
            "research_not_production",
        }
        if self.research.confirmation_mode is ResearchConfirmationMode.AUTO_ACCEPT:
            labels.add("auto_accept_selection")
        if any(value == "gap" for value in tool_status.values()):
            labels.add("tool_gap")
        if self.universe_source != "explicit":
            labels.add("local_coverage_universe")
        return {
            "mode": "historical_agent_research",
            "labels": sorted(labels),
            "model_ref": self.llm.model_ref,
            "requested_model_ref": self.llm.requested_model_ref,
            "prompt_version": self.research.prompt_version,
            "plan_version": self.research.plan_version,
            "decision_dates": [item.isoformat() for item in self.decision_dates],
            "universe_mode": self.research.universe_mode.value,
            "universe_source": self.universe_source,
            "confirmation_mode": self.research.confirmation_mode.value,
            "universe": self.universe,
            "universe_size": len(self.universe),
            "llm_calls": self.llm.llm_call_count,
            "cache_hits": self.llm.cache_hits,
            "call_records": [record.to_dict() for record in self.llm.calls],
            "llm_errors": self.llm.errors,
            "budget_exhausted": self._budget_exhausted,
            "max_llm_calls": self.research.max_llm_calls,
            "tool_status": tool_status,
            "decisions": self.decisions,
            "skipped": self.skipped,
        }

    def close(self) -> None:
        self.cache.close()

    def _filter_one_lot_executable(
        self,
        snapshots: dict[str, Any],
        portfolio: dict[str, Any],
        session: date,
        as_of: date,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Drop candidates that cannot buy one A-share lot under current cash/risk.

        This runs before the selection prompt, so the Agent does not waste a
        ranking slot on a symbol that the execution engine can never size.
        """
        policy = self.config.risk_policy
        if policy is None:
            return snapshots, []
        equity = Decimal(str(portfolio.get("equity") or 0))
        available = Decimal(str(portfolio.get("available_cash") or 0))
        if equity <= 0 or available <= 0:
            return {}, [
                {"reason": "no_equity_or_cash", "symbol": symbol}
                for symbol in sorted(snapshots)
            ]
        positions = portfolio.get("positions") or []
        if len(positions) >= policy.max_concurrent_positions:
            return {}, [
                {"reason": "max_positions_reached", "symbol": symbol}
                for symbol in sorted(snapshots)
            ]
        if Decimal(str(portfolio.get("exposure_pct") or 0)) >= policy.effective_total_exposure_pct:
            return {}, [
                {"reason": "max_exposure_reached", "symbol": symbol}
                for symbol in sorted(snapshots)
            ]
        risk_budget = equity * policy.risk_per_trade_pct / Decimal("100")
        executable: dict[str, Any] = {}
        skipped: list[dict[str, Any]] = []
        for symbol, snapshot in snapshots.items():
            rule = self.market.rule(symbol, session)
            draft = build_research_plan(
                config=self.config,
                snapshot=snapshot,
                rule=rule,
                available_at=self.calendar.session_close(as_of),
                valid_from=session,
                rank=1,
            )
            if draft is None:
                skipped.append({"symbol": symbol, "reason": "research_plan_unavailable"})
                continue
            plan = draft.plan
            lot = max(1, int(rule.buy_lot_size))
            one_lot_cost = plan.entry_high * Decimal(lot)
            one_lot_risk = Decimal(lot) * (plan.entry_high - plan.stop_loss)
            target_pct = min(
                plan.target_position_pct,
                policy.max_single_position_pct,
                self.config.max_single_position_pct,
            )
            target_budget = equity * target_pct / Decimal("100")
            if one_lot_cost > available:
                skipped.append({"symbol": symbol, "reason": "one_lot_cash_exceeds_available"})
                continue
            if one_lot_cost > target_budget:
                skipped.append({"symbol": symbol, "reason": "one_lot_exceeds_target_position"})
                continue
            if one_lot_risk > risk_budget:
                skipped.append({"symbol": symbol, "reason": "one_lot_risk_exceeds_budget"})
                continue
            snapshot.one_lot_cost = one_lot_cost
            snapshot.one_lot_risk_pct = float(one_lot_risk / equity * Decimal("100"))
            executable[symbol] = snapshot
        return executable, skipped

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _decision_dates(self) -> list[date]:
        sessions = self.calendar.sessions(self.config.start_date, self.config.end_date)
        result: list[date] = []
        seen: set[tuple[int, int]] = set()
        for session in sessions:
            key = (session.year, session.month)
            if key in seen:
                continue
            seen.add(key)
            result.append(session)
        return result

    def _can_afford(self, portfolio: dict[str, Any], plan: ReplayPlan, lot_size: int) -> bool:
        available = Decimal(str(portfolio.get("available_cash") or 0))
        if available <= 0:
            return False
        policy = self.config.risk_policy
        positions = portfolio.get("positions") or []
        if policy is not None:
            if len(positions) >= policy.max_concurrent_positions:
                return False
            exposure = Decimal(str(portfolio.get("exposure_pct") or 0))
            if exposure >= policy.effective_total_exposure_pct:
                return False
        lot = max(1, int(lot_size))
        one_lot_cost = plan.entry_high * Decimal(lot)
        if one_lot_cost <= 0:
            return False
        return available >= one_lot_cost

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(event, payload)
        except Exception:
            logger.warning("research progress callback failed", exc_info=True)


__all__ = ["ResearchOrchestrator"]
