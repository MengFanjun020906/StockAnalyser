# -*- coding: utf-8 -*-
"""Application service for single-stock entry-signal lifecycle and previews."""

from __future__ import annotations

import re
import hashlib
import json
import logging
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select

from data_provider.base import canonical_stock_code, normalize_stock_code
from src.agent.risk_gate import QuoteState, RiskGateEvaluator, RiskGateInput
from src.agent.pricing.repository import PricePlanRepository
from src.agent.pricing.schemas import PricePlanArtifact, ScenarioRole, ZoneScenario
from src.core.trading_calendar import get_effective_trading_date, get_market_for_stock, is_market_open
from src.data.stock_index_loader import get_index_stock_name
from src.data.stock_mapping import STOCK_NAME_MAP, is_meaningful_stock_name
from src.repositories.signal_lifecycle_repo import (
    ACTIVE_SIGNAL_STATUSES,
    SignalLifecycleRepository,
)
from src.schemas.agent_signal import TradePlan
from src.services.signal_market_data_service import SignalMarketDataService
from src.storage import PortfolioPosition


logger = logging.getLogger(__name__)


ENTRY_INTENT_PATTERN = re.compile(
    r"买入|买点|入场|建仓|开仓|加仓|是否.*买|适合.*买|entry|buy|open\s+position|add\s+position",
    flags=re.IGNORECASE,
)


def _float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if float(value) > 0 else None
    text = str(value).replace(",", "").replace("，", "").strip()
    if not text or text.upper() in {"N/A", "NA", "NONE", "--"}:
        return None
    numbers = re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?", text)
    if not numbers:
        return None
    try:
        number = float(numbers[0])
    except ValueError:
        return None
    return number if number > 0 else None


def _datetime(value: Any, *, fallback: Optional[datetime] = None) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            pass
    return fallback or datetime.now()


def _stock_identity(item: Dict[str, Any]) -> Tuple[str, str]:
    """Return the stable list identity used by Signal Preview."""
    raw_code = str(item.get("stock_code") or "").strip()
    code = canonical_stock_code(normalize_stock_code(raw_code)) or raw_code.upper()
    return str(item.get("market") or "cn").strip().lower(), code


def _latest_per_stock(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the first row per market/symbol from repositories sorted newest-first."""
    latest: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for item in items:
        identity = _stock_identity(item)
        if identity in seen:
            continue
        seen.add(identity)
        latest.append(item)
    return latest


def _with_display_stock_name(item: Dict[str, Any]) -> Dict[str, Any]:
    """Fill a missing display name from the existing offline stock indexes."""
    payload = dict(item)
    code = str(payload.get("stock_code") or "").strip()
    current = payload.get("stock_name")
    if is_meaningful_stock_name(current, code):
        payload["stock_name"] = str(current).strip()
        return payload

    normalized = canonical_stock_code(normalize_stock_code(code)) or code.upper()
    for candidate in (STOCK_NAME_MAP.get(normalized), get_index_stock_name(normalized)):
        if is_meaningful_stock_name(candidate, normalized):
            payload["stock_name"] = str(candidate).strip()
            break
    else:
        payload["stock_name"] = None
    return payload


class SignalLifecycleService:
    """Create, reconcile, and query user-facing lifecycle records."""

    def __init__(
        self,
        repository: Optional[SignalLifecycleRepository] = None,
        market_data: Optional[SignalMarketDataService] = None,
        price_plan_repository: Optional[PricePlanRepository] = None,
    ) -> None:
        self.repo = repository or SignalLifecycleRepository()
        self.market_data = market_data or SignalMarketDataService(self.repo.db)
        self.price_plans = price_plan_repository or PricePlanRepository(self.repo.db)

    def capture_agent_result(
        self,
        *,
        message: str,
        session_id: str,
        result: Any,
        context: Optional[Dict[str, Any]] = None,
        composite_payload: Optional[Dict[str, Any]] = None,
        risk_gate_payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Persist a complete single-stock entry or observation plan."""
        if result is None or not bool(getattr(result, "success", False)):
            return None
        if isinstance(getattr(result, "stock_selection", None), dict):
            return None
        context = dict(context or {})
        dashboard = getattr(result, "dashboard", None)
        symbol = self._resolve_symbol(message, context)
        price_plan = self._resolve_price_plan_artifact(
            result=result,
            risk_gate_payload=risk_gate_payload,
            symbol=symbol,
        )
        if not symbol and price_plan is not None:
            symbol = price_plan.symbol
        trade_plan = self._resolve_agent_trade_plan(
            result=result,
            risk_gate_payload=risk_gate_payload,
            symbol=symbol,
        )
        if not symbol and trade_plan is not None:
            symbol = canonical_stock_code(normalize_stock_code(trade_plan.symbol)) or trade_plan.symbol.upper()
        if not symbol:
            dashboard_symbol = self._dashboard_value(dashboard, "stock_code") or self._dashboard_value(dashboard, "symbol")
            symbol = canonical_stock_code(normalize_stock_code(str(dashboard_symbol or ""))) or str(dashboard_symbol or "").upper()
        if not symbol or not self._should_capture_plan(message, context):
            return None
        context_position = self._context_position(context, symbol)
        persisted_position = self._persisted_position(symbol)
        has_position = context_position or persisted_position
        add_intent = bool(re.search(r"加仓|add\s+position", message, flags=re.IGNORECASE))
        if has_position and not add_intent:
            return None
        signal_type = "add_position" if add_intent else "entry"
        expected_action = "add" if add_intent else "open"
        account_decision = self._resolve_account_decision(
            result=result,
            risk_gate_payload=risk_gate_payload,
        )
        selected_scenario = self._select_price_plan_scenario(
            price_plan,
            selected_scenario_id=self._resolve_selected_scenario_id(
                result=result,
                risk_gate_payload=risk_gate_payload,
            ),
        )
        if selected_scenario is not None and price_plan is not None:
            trade_plan = self._project_scenario_trade_plan(
                artifact=price_plan,
                scenario=selected_scenario,
                action=expected_action,
            )
        zones = (
            self._artifact_zones(price_plan)
            if price_plan is not None
            else self._trade_plan_zones(trade_plan)
            if trade_plan is not None
            else []
        )
        points = (
            self._scenario_points(selected_scenario)
            if selected_scenario is not None
            else self._trade_plan_points(trade_plan)
            if trade_plan is not None
            else self._dashboard_sniper_points(dashboard)
        )
        if price_plan is not None and selected_scenario is None:
            # Observation artifacts remain visible through the plan endpoint,
            # but never become executable EntrySignal rows.
            return None
        if not zones:
            entry_values = [value for value in (points.get("ideal_buy"), points.get("secondary_buy")) if value]
            if entry_values:
                zones = [{"label": "entry", "low": min(entry_values), "high": max(entry_values)}]
        if not zones:
            return None
        selected_zone = (
            next(
                (
                    zone
                    for zone in zones
                    if selected_scenario is not None
                    and zone.get("scenario_id") == selected_scenario.scenario_id
                ),
                zones[0],
            )
        )
        entry_low = float(selected_zone["low"])
        entry_high = float(selected_zone["high"])
        stop_price = points.get("stop_loss")
        invalidation = (
            [rule.rule_type for rule in selected_scenario.invalidation_rules]
            if selected_scenario is not None
            else list(trade_plan.invalidation_conditions)
            if trade_plan is not None
            else self._dashboard_invalidation_conditions(dashboard)
        )
        if stop_price is None and not invalidation:
            return None
        current_action = (
            "wait"
            if selected_scenario is not None
            else
            str(trade_plan.current_action or trade_plan.action)
            if trade_plan is not None
            else self._dashboard_action(dashboard)
        )
        trigger_action = expected_action if selected_scenario is not None else str(trade_plan.action) if trade_plan is not None else (
            expected_action if current_action == "wait" else current_action
        )
        if trigger_action != expected_action:
            return None
        request_as_of = _datetime(context.get("as_of")) if context.get("as_of") else datetime.now()
        market = str(context.get("market") or get_market_for_stock(symbol) or "cn").lower()
        if not isinstance(composite_payload, dict):
            try:
                local_chart = self.market_data.load_chart(
                    symbol,
                    market=market,
                    refresh_tail=False,
                    as_of=request_as_of.date(),
                    holding=bool(has_position),
                )
                local_composite = local_chart.get("composite") if isinstance(local_chart, dict) else None
                if isinstance(local_composite, dict):
                    composite_payload = local_composite
            except Exception as exc:
                logger.warning("Local composite snapshot unavailable for %s: %s", symbol, exc)
                composite_payload = None
        snapshot = composite_payload.get("snapshot") if isinstance(composite_payload, dict) else {}
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        as_of = _datetime(snapshot.get("as_of"), fallback=request_as_of)

        gate = self._resolve_risk_gate(
            expected_action=expected_action,
            symbol=symbol,
            stop_price=stop_price,
            invalidation=invalidation,
            result=result,
            risk_gate_payload=risk_gate_payload,
            trade_plan=trade_plan,
        )
        gate_blocked = gate.get("status") == "blocked" or gate.get("allowed_action") not in {"open", "add"}
        decision_status = str(account_decision.get("execution_status") or "")
        decision_allows_execution = decision_status == "actionable"
        risk_blocked = gate_blocked or decision_status == "risk_blocked"
        if gate_blocked:
            if gate.get("status") == "blocked":
                self._invalidate_existing_for_analysis(
                    symbol,
                    as_of=as_of,
                    session_id=session_id,
                    reason="risk_gate_blocked",
                )

        composite_verdict = (
            composite_payload.get("composite_verdict")
            if isinstance(composite_payload, dict) and isinstance(composite_payload.get("composite_verdict"), dict)
            else {}
        )
        exit_warning = composite_verdict.get("exit_warning") if isinstance(composite_verdict.get("exit_warning"), dict) else {}
        exit_warning_blocked = int(exit_warning.get("level") or 0) >= 3
        if exit_warning_blocked:
            self._invalidate_existing_for_analysis(
                symbol,
                as_of=as_of,
                session_id=session_id,
                reason="exit_warning_level_3",
            )
        valid_days = 5
        valid_until = self._add_trading_days(as_of.date(), valid_days, market)
        owner_id, _ = self._context_owner_account(context, symbol)
        # Entry signals are account-neutral. Holdings are consulted only to
        # distinguish an explicit add-position request from a new entry.
        account_id = None
        setup_type = self._setup_type(composite_payload, dashboard, entry_low=entry_low, entry_high=entry_high)
        signal_id = f"{symbol}-{signal_type}-{as_of:%Y%m%d}-{uuid.uuid4().hex[:8]}"
        evidence_quality = "ok"
        snapshot_quality = str(snapshot.get("data_quality") or "").lower()
        freshness = str(snapshot.get("freshness") or "").lower()
        request_time = request_as_of
        expected_trading_date = get_effective_trading_date(market, current_time=request_time)
        snapshot_date = _datetime(snapshot.get("as_of")).date() if snapshot.get("as_of") else None
        stale_snapshot = bool(snapshot_date and snapshot_date < expected_trading_date)
        if (
            not snapshot
            or snapshot_quality in {"limited", "insufficient", "unknown", "partial", "failed"}
            or freshness in {"unknown", "stale", "unavailable", "conflicted"}
            or stale_snapshot
        ):
            evidence_quality = "partial"
        stock_name = str(context.get("stock_name") or self._dashboard_value(dashboard, "stock_name") or "").strip() or None

        policy_version = "signal-lifecycle.v2"
        structure_version = "chan-structure.v2"
        evidence_payload = {
            "snapshot": snapshot,
            "composite_verdict": composite_verdict,
            "risk_gate": gate,
            "dashboard": dashboard,
            "trade_plan": trade_plan.model_dump(mode="json") if trade_plan is not None else None,
            "price_plan": price_plan.model_dump(mode="json") if price_plan is not None else None,
        }
        evidence_fingerprint = hashlib.sha256(
            json.dumps(evidence_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        valid_days = 10 if setup_type == "trend_pullback" else valid_days
        if decision_status in {"observe_only", "vetoed", "closed", "no_plan"}:
            plan_mode = "observation_only"
        elif decision_status in {"review_pending", "waiting_confirmation"}:
            plan_mode = "waiting_confirmation"
        elif risk_blocked or exit_warning_blocked:
            plan_mode = "risk_blocked"
        elif current_action == "reject":
            plan_mode = "observation_only"
        elif current_action == "wait":
            plan_mode = "conditional_entry"
        else:
            plan_mode = "actionable_entry"
        execution_eligible = plan_mode in {"conditional_entry", "actionable_entry"}
        if price_plan is not None:
            execution_eligible = execution_eligible and decision_allows_execution
        status = "needs_review" if evidence_quality != "ok" else (
            "proposed" if not execution_eligible else "active"
        )
        return self.repo.upsert_entry_plan(
            {
                "signal_id": signal_id,
                "plan_id": price_plan.plan_id if price_plan is not None else None,
                "scenario_id": selected_scenario.scenario_id if selected_scenario is not None else None,
                "owner_id": owner_id,
                "account_id": account_id,
                "stock_code": symbol,
                "stock_name": stock_name,
                "market": market,
                "signal_type": signal_type,
                # Score levels are observations, not setup identities. Keeping the
                # setup stable lets a later query revise the same lifecycle record.
                "setup_type": setup_type,
                "direction": "long",
                "status": status,
                "entry_low": entry_low,
                "entry_high": entry_high,
                "stop_price": stop_price,
                "trigger_rule": {
                    "type": "price_entered_zone",
                    "low": entry_low,
                    "high": entry_high,
                    "zones": zones,
                    "stop_trigger_mode": "daily_close",
                },
                "invalidation_rules": invalidation or ([f"daily_close_below_{stop_price}"] if stop_price else []),
                "created_as_of": as_of,
                "last_checked_as_of": as_of,
                "valid_trading_days": valid_days,
                "valid_until": valid_until,
                "evidence_quality": evidence_quality,
                "source_trace_id": session_id,
                "metadata": {
                    "current_action": current_action,
                    "trigger_action": trigger_action,
                    "plan_mode": plan_mode,
                    "execution_status": decision_status or None,
                    "execution_eligible": execution_eligible,
                    "risk_gate": gate,
                    "composite_verdict": composite_verdict,
                    "trade_plan": trade_plan.model_dump(mode="json") if trade_plan is not None else None,
                    "price_plan_id": price_plan.plan_id if price_plan is not None else None,
                    "selected_scenario_id": selected_scenario.scenario_id if selected_scenario is not None else None,
                    "price_source": (
                        "deterministic_price_plan" if price_plan is not None else "legacy_unstructured"
                    ),
                    "price_plan_algorithm_version": (
                        price_plan.algorithm_version if price_plan is not None else None
                    ),
                    "price_plan_params_version": (
                        price_plan.params_version if price_plan is not None else None
                    ),
                    "price_plan_fingerprint": (
                        price_plan.plan_fingerprint if price_plan is not None else None
                    ),
                    "account_decision": account_decision or None,
                    "source": "agent_result",
                    "policy_version": policy_version,
                    "structure_version": structure_version,
                    "evidence_fingerprint": evidence_fingerprint,
                    "evidence_snapshot": {
                        "as_of": snapshot.get("as_of") or context.get("as_of"),
                        "data_quality": snapshot.get("data_quality"),
                        "expected_trading_date": expected_trading_date.isoformat(),
                        "stale_snapshot": stale_snapshot,
                        "setup_type": setup_type,
                    },
                },
            },
            dedupe_key=f"agent-result:{session_id}:{symbol}:{signal_type}",
        )

    @classmethod
    def is_entry_request(cls, *, message: str, context: Optional[Dict[str, Any]] = None) -> bool:
        """Return whether a request should ask the Agent for a machine trade-plan sidecar."""
        return cls._has_entry_intent(message, dict(context or {}))

    @classmethod
    def should_request_plan(cls, *, message: str, context: Optional[Dict[str, Any]] = None) -> bool:
        """Return whether a request may yield a single-stock preview plan."""
        request_context = dict(context or {})
        if cls._has_entry_intent(message, request_context):
            return True
        report = cls._report_context(request_context)
        if report is None:
            return True
        intent = str(cls._object_value(report, "intent") or "").lower()
        include_entry_plan = cls._object_value(report, "include_entry_plan")
        return include_entry_plan is not False and intent in {"auto", "qa", "entry_analysis"}

    def list_preview(self, *, status: Optional[str] = None, symbol: Optional[str] = None) -> Dict[str, Any]:
        statuses: Optional[Iterable[str]] = None
        if status and status != "all":
            statuses = [status]
        entries = self.repo.list_entry_signals(statuses=statuses, stock_code=symbol, limit=300)
        today = date.today()
        entries = _latest_per_stock(
            _with_display_stock_name(self._with_effective_entry_status(item, today=today))
            for item in entries
        )
        artifacts = self.price_plans.list_artifacts(symbol=symbol, limit=300)
        price_plans = _latest_per_stock(self._price_plan_summary(item) for item in artifacts)
        watches = _latest_per_stock(
            _with_display_stock_name(item)
            for item in self.repo.list_position_watches(
                statuses=None if status in (None, "all") else [status],
                limit=300,
            )
        )
        risk_order = {"critical": 0, "high": 1, "medium": 2, "unknown": 3, "low": 4}
        watches.sort(key=lambda item: (risk_order.get(str(item.get("risk_level")), 5), -(item.get("version") or 0)))
        scheduler = self.repo.list_scheduler_states()
        now = datetime.utcnow()
        for item in scheduler:
            heartbeat = item.get("heartbeat_at")
            item["online"] = bool(heartbeat and (now - heartbeat).total_seconds() <= 900)
        return {
            "entry_signals": entries,
            "price_plans": price_plans,
            "position_watches": watches,
            "scheduler": scheduler,
            "counts": {
                "entry": len(entries),
                "plans": len(price_plans),
                "active_entry": sum(item.get("effective_status") in ACTIVE_SIGNAL_STATUSES for item in entries),
                "positions": sum(item.get("status") != "closed" for item in watches),
                "high_risk": sum(item.get("risk_level") in {"high", "critical"} for item in watches),
            },
        }

    def _invalidate_existing_for_analysis(
        self,
        symbol: str,
        *,
        as_of: datetime,
        session_id: str,
        reason: str,
    ) -> None:
        """Invalidate active entry plans from an explicit hard-stop analysis."""
        active = self.repo.list_entry_signals(
            statuses=ACTIVE_SIGNAL_STATUSES,
            stock_code=symbol,
            limit=50,
        )
        for item in active:
            try:
                self.repo.transition_entry_signal(
                    item["signal_id"],
                    to_status="invalidated",
                    event_type="signal_invalidated",
                    occurred_at=as_of,
                    as_of=as_of,
                    source="single_stock_query",
                    dedupe_key=f"analysis-invalidated:{session_id}:{item['signal_id']}:{reason}",
                    payload={"reason": reason, "source_trace_id": session_id},
                )
            except ValueError:
                # A concurrent request may have already moved this plan to a
                # terminal state; the append-only event from that request wins.
                continue

    def reconcile_for_request(
        self,
        *,
        message: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Advance existing signals for an explicit single-stock request."""
        request_context = dict(context or {})
        symbol = self._resolve_symbol(message, request_context)
        if not symbol:
            return []
        active = self.repo.list_entry_signals(
            statuses=ACTIVE_SIGNAL_STATUSES,
            stock_code=symbol,
            limit=20,
        )
        reconciled: List[Dict[str, Any]] = []
        for item in active:
            detail = self.entry_chart(item["signal_id"], refresh_tail=True)
            if detail is not None:
                detail["subject"] = self._reconcile_entry(item, detail["chart"])
                detail["events"] = self.repo.list_signal_events(item["signal_id"])
                reconciled.append(detail["subject"])
        return reconciled

    def entry_chart(self, signal_id: str, *, refresh_tail: bool = True) -> Optional[Dict[str, Any]]:
        signal = self.repo.get_entry_signal(signal_id)
        if signal is None:
            return None
        signal = _with_display_stock_name(self._with_effective_entry_status(signal))
        chart = self.market_data.load_chart(
            signal["stock_code"],
            market=signal["market"],
            account_id=signal.get("account_id"),
            refresh_tail=refresh_tail,
            holding=False,
        )
        return {
            "kind": "entry_signal",
            "subject": signal,
            "events": self.repo.list_signal_events(signal_id),
            "chart": chart,
        }

    def price_plan_chart(self, plan_id: str, *, refresh_tail: bool = True) -> Optional[Dict[str, Any]]:
        artifact = self.price_plans.get_artifact(plan_id)
        if artifact is None:
            return None
        chart = self.market_data.load_chart(
            artifact.symbol,
            market=artifact.market,
            refresh_tail=refresh_tail,
            holding=False,
            as_of=None,
        )
        lifecycle_states = self.price_plans.reconcile_lifecycle(
            artifact,
            list(chart.get("bars") or []),
        )
        return {
            "kind": "price_plan",
            "subject": self._price_plan_summary(artifact),
            "price_plan": artifact.model_dump(mode="json"),
            "review": self.price_plans.get_latest_review(artifact.plan_id),
            "account_decision": self.price_plans.get_latest_account_decision(artifact.plan_id),
            "lifecycle_states": [
                item.model_dump(mode="json")
                for item in lifecycle_states
            ],
            "events": self.price_plans.list_lifecycle_events(plan_id),
            "chart": chart,
        }

    @staticmethod
    def _with_effective_entry_status(
        signal: Dict[str, Any],
        *,
        today: Optional[date] = None,
    ) -> Dict[str, Any]:
        payload = dict(signal)
        current_date = today or date.today()
        payload["effective_status"] = (
            "expired"
            if payload.get("status") in ACTIVE_SIGNAL_STATUSES
            and payload.get("valid_until")
            and payload["valid_until"] < current_date
            else payload.get("status")
        )
        return payload

    def position_chart(self, watch_id: str, *, refresh_tail: bool = True) -> Optional[Dict[str, Any]]:
        watch = self.repo.get_position_watch(watch_id)
        if watch is None:
            return None
        watch = _with_display_stock_name(watch)
        return {
            "kind": "position_watch",
            "subject": watch,
            "events": self.repo.list_position_watch_events(watch_id),
            "chart": self.market_data.load_chart(
                watch["stock_code"],
                market=watch["market"],
                account_id=watch["account_id"],
                refresh_tail=refresh_tail,
                holding=True,
            ),
        }

    def _reconcile_entry(self, signal: Dict[str, Any], chart: Dict[str, Any]) -> Dict[str, Any]:
        if signal["status"] not in ACTIVE_SIGNAL_STATUSES:
            return signal
        if chart.get("status") != "ok":
            if signal["status"] == "needs_review":
                return signal
            target = _datetime((chart.get("data_window") or {}).get("target_date"))
            return self.repo.transition_entry_signal(
                signal["signal_id"],
                to_status="needs_review",
                event_type="signal_checked",
                occurred_at=target,
                as_of=target,
                source="single_stock_query",
                dedupe_key=f"{signal['signal_id']}:needs_review:{target.date().isoformat()}",
                payload={"reason": "market_data_unavailable", "data_window": chart.get("data_window")},
            )
        last_checked = _datetime(signal.get("last_checked_as_of"))
        bars = [
            item for item in chart.get("bars") or []
            if _datetime(item.get("date")).date() > last_checked.date()
        ]
        for bar in bars:
            bar_date = _datetime(bar.get("date"))
            high = _float(bar.get("high"))
            low = _float(bar.get("low"))
            close = _float(bar.get("close"))
            matched_zone = next(
                (
                    zone for zone in self._signal_entry_zones(signal)
                    if (
                        high is not None
                        and low is not None
                        and low <= float(zone["high"])
                        and high >= float(zone["low"])
                    )
                ),
                None,
            )
            hit_entry = matched_zone is not None
            hit_stop = close is not None and signal.get("stop_price") is not None and close <= float(signal["stop_price"])
            stop_mode = str((signal.get("trigger_rule") or {}).get("stop_trigger_mode") or "daily_close")
            if hit_entry and hit_stop and stop_mode == "intraday":
                return self._resolve_intraday_conflict(signal, bar, bar_date)
            if hit_entry and hit_stop:
                # A daily candle cannot establish intraday order. Preserve the
                # zone touch before applying the close-based stop rule.
                if signal["status"] == "active":
                    signal = self.repo.transition_entry_signal(
                        signal["signal_id"],
                        to_status="price_triggered",
                        event_type="entry_price_triggered",
                        occurred_at=bar_date,
                        as_of=bar_date,
                        source="lazy_reconcile",
                        dedupe_key=f"{signal['signal_id']}:entry_price_triggered:{bar_date.date().isoformat()}",
                        payload={"bar": bar, "entry_low": signal["entry_low"], "entry_high": signal["entry_high"], "matched_zone": matched_zone, "path": "daily_candle_touch"},
                    )
                return self.repo.transition_entry_signal(
                    signal["signal_id"],
                    to_status="invalidated",
                    event_type="signal_invalidated",
                    occurred_at=bar_date,
                    as_of=bar_date,
                    source="lazy_reconcile",
                    dedupe_key=f"{signal['signal_id']}:invalidated:{bar_date.date().isoformat()}",
                    payload={"bar": bar, "rule": "daily_close_below_stop", "path": "daily_candle_conflict"},
                )
            if hit_stop:
                invalidated = self.repo.transition_entry_signal(
                    signal["signal_id"],
                    to_status="invalidated",
                    event_type="signal_invalidated",
                    occurred_at=bar_date,
                    as_of=bar_date,
                    source="lazy_reconcile",
                    dedupe_key=f"{signal['signal_id']}:invalidated:{bar_date.date().isoformat()}",
                    payload={"bar": bar, "rule": "daily_close_below_stop"},
                )
                return invalidated
            if hit_entry:
                return self.repo.transition_entry_signal(
                    signal["signal_id"],
                    to_status="price_triggered",
                    event_type="entry_price_triggered",
                    occurred_at=bar_date,
                    as_of=bar_date,
                    source="lazy_reconcile",
                    dedupe_key=f"{signal['signal_id']}:entry_price_triggered:{bar_date.date().isoformat()}",
                    payload={"bar": bar, "entry_low": signal["entry_low"], "entry_high": signal["entry_high"], "matched_zone": matched_zone},
                )

        valid_until = signal.get("valid_until")
        chart_end = _datetime((chart.get("data_window") or {}).get("end"))
        if valid_until and chart_end.date() >= valid_until:
            occurred = datetime.combine(valid_until, time.max)
            return self.repo.transition_entry_signal(
                signal["signal_id"],
                to_status="expired",
                event_type="signal_expired",
                occurred_at=occurred,
                as_of=chart_end,
                source="lazy_reconcile",
                dedupe_key=f"{signal['signal_id']}:expired:{valid_until.isoformat()}",
                payload={"valid_until": valid_until.isoformat()},
            )
        if chart_end > last_checked:
            return self.repo.transition_entry_signal(
                signal["signal_id"],
                to_status=signal["status"],
                event_type="signal_checked",
                occurred_at=chart_end,
                as_of=chart_end,
                source="single_stock_query",
                dedupe_key=f"{signal['signal_id']}:checked:{chart_end.isoformat()}",
                payload={"data_window": chart.get("data_window"), "data_quality": chart.get("status")},
            )
        return signal

    @staticmethod
    def _signal_entry_zones(signal: Dict[str, Any]) -> List[Dict[str, Any]]:
        trigger_rule = signal.get("trigger_rule") if isinstance(signal.get("trigger_rule"), dict) else {}
        raw_zones = trigger_rule.get("zones") if isinstance(trigger_rule, dict) else None
        zones: List[Dict[str, Any]] = []
        for raw in raw_zones or []:
            if not isinstance(raw, dict):
                continue
            low = _float(raw.get("low"))
            high = _float(raw.get("high"))
            if low is None or high is None:
                continue
            zones.append({
                "label": str(raw.get("label") or "entry"),
                "low": min(low, high),
                "high": max(low, high),
                **({"rationale": str(raw["rationale"])} if raw.get("rationale") else {}),
            })
        if zones:
            return zones
        low = _float(signal.get("entry_low"))
        high = _float(signal.get("entry_high"))
        if low is None or high is None:
            return []
        return [{"label": "entry", "low": min(low, high), "high": max(low, high)}]

    def _resolve_intraday_conflict(
        self,
        signal: Dict[str, Any],
        bar: Dict[str, Any],
        bar_date: datetime,
    ) -> Dict[str, Any]:
        """Resolve an explicitly intraday stop conflict from cached 5m bars."""
        from sqlalchemy import select
        from src.storage import StockMinuteBar

        with self.repo.db.get_session() as session:
            minute_bars = session.execute(
                select(StockMinuteBar)
                .where(
                    StockMinuteBar.code == signal["stock_code"],
                    StockMinuteBar.bar_date == bar_date.date(),
                    StockMinuteBar.frequency == "5",
                    StockMinuteBar.adjustflag == "3",
                )
                .order_by(StockMinuteBar.bar_datetime)
            ).scalars().all()
        if not minute_bars and signal.get("market") == "cn" and is_market_open("cn", bar_date.date()):
            # A-share conflicts may be resolved with a bounded 5m fetch. HK/US
            # remain local-only here; without minute evidence they stay
            # ambiguous rather than guessing an order sequence.
            from src.services.agent_entry_minute_data_service import AgentEntryMinuteDataService

            AgentEntryMinuteDataService(db_manager=self.repo.db).ensure_symbol_day(
                signal["stock_code"],
                bar_date.date(),
                frequency="5",
                adjustflag="3",
            )
            with self.repo.db.get_session() as session:
                minute_bars = session.execute(
                    select(StockMinuteBar)
                    .where(
                        StockMinuteBar.code == signal["stock_code"],
                        StockMinuteBar.bar_date == bar_date.date(),
                        StockMinuteBar.frequency == "5",
                        StockMinuteBar.adjustflag == "3",
                    )
                    .order_by(StockMinuteBar.bar_datetime)
                ).scalars().all()
        if not minute_bars:
            return self.repo.transition_entry_signal(
                signal["signal_id"],
                to_status="ambiguous",
                event_type="signal_ambiguous",
                occurred_at=bar_date,
                as_of=bar_date,
                source="single_stock_query",
                dedupe_key=f"{signal['signal_id']}:ambiguous:{bar_date.date().isoformat()}",
                payload={"bar": bar, "reason": "minute_bars_missing"},
            )
        stop = float(signal["stop_price"])
        first_entry = None
        first_entry_zone = None
        first_stop = None
        for minute in minute_bars:
            low = _float(minute.low)
            high = _float(minute.high)
            if first_entry is None and low is not None and high is not None:
                first_entry_zone = next(
                    (
                        zone for zone in self._signal_entry_zones(signal)
                        if low <= float(zone["high"]) and high >= float(zone["low"])
                    ),
                    None,
                )
                if first_entry_zone is not None:
                    first_entry = minute
            if first_stop is None and low is not None and low <= stop:
                first_stop = minute
        if first_stop is not None and (first_entry is None or first_stop.bar_datetime <= first_entry.bar_datetime):
            return self.repo.transition_entry_signal(
                signal["signal_id"],
                to_status="invalidated",
                event_type="signal_invalidated",
                occurred_at=first_stop.bar_datetime,
                as_of=bar_date,
                source="single_stock_query",
                dedupe_key=f"{signal['signal_id']}:intraday_invalidated:{bar_date.date().isoformat()}",
                payload={"minute_bar": first_stop.to_dict(), "rule": "intraday_stop"},
            )
        return self.repo.transition_entry_signal(
            signal["signal_id"],
            to_status="price_triggered",
            event_type="entry_price_triggered",
            occurred_at=first_entry.bar_datetime if first_entry is not None else bar_date,
            as_of=bar_date,
            source="single_stock_query",
            dedupe_key=f"{signal['signal_id']}:intraday_triggered:{bar_date.date().isoformat()}",
                payload={"minute_bar": first_entry.to_dict() if first_entry is not None else bar, "matched_zone": first_entry_zone, "rule": "intraday_entry"},
        )

    @staticmethod
    def _resolve_symbol(message: str, context: Dict[str, Any]) -> str:
        explicit = str(context.get("stock_code") or "").strip().upper()
        if explicit:
            return explicit
        match = re.search(r"(?<!\d)(?:SH|SZ|BJ)?(\d{6})(?:\.(?:SH|SZ|BJ|SS))?(?!\d)", message, flags=re.I)
        if match:
            return match.group(1)
        match = re.search(r"\b(?:HK)?(\d{5})\b|\b([A-Z]{1,5}(?:\.[A-Z])?)\b", message.upper())
        return next((value for value in match.groups() if value), "") if match else ""

    @classmethod
    def _setup_type(
        cls,
        composite_payload: Optional[Dict[str, Any]],
        dashboard: Any,
        *,
        entry_low: float,
        entry_high: float,
    ) -> str:
        snapshot = composite_payload.get("snapshot") if isinstance(composite_payload, dict) else {}
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        chan = snapshot.get("chan") if isinstance(snapshot.get("chan"), dict) else {}
        signals = chan.get("signals") or []
        names = {str(item.get("name")) for item in signals if isinstance(item, dict) and item.get("triggered")}
        for name, setup in (
            ("first_buy", "chan_first_buy"),
            ("second_buy", "chan_second_buy"),
            ("third_buy", "chan_third_buy"),
            ("center_breakout", "center_breakout"),
        ):
            if name in names:
                return setup
        current_price = _float(cls._dashboard_value(dashboard, "current_price"))
        if current_price is None:
            current_price = _float(cls._dashboard_value(dashboard, "last_price"))
        if current_price is None:
            return "agent_entry"
        if entry_high < current_price:
            return "trend_pullback"
        if entry_low > current_price:
            return "breakout"
        return "spot_entry"

    @staticmethod
    def _has_entry_intent(message: str, context: Dict[str, Any]) -> bool:
        return bool(context.get("signal_lifecycle_entry_intent")) or bool(
            ENTRY_INTENT_PATTERN.search(message or "")
        )

    @classmethod
    def _should_capture_plan(cls, message: str, context: Dict[str, Any]) -> bool:
        return bool(context.get("signal_lifecycle_plan_candidate")) or cls.should_request_plan(
            message=message,
            context=context,
        )

    @staticmethod
    def _object_value(value: Any, key: str) -> Any:
        return value.get(key) if isinstance(value, dict) else getattr(value, key, None)

    @classmethod
    def _report_context(cls, context: Dict[str, Any]) -> Any:
        agent_context = context.get("agent_user_context")
        return cls._object_value(agent_context, "report")

    @staticmethod
    def _dashboard_value(dashboard: Any, key: str) -> Any:
        if not isinstance(dashboard, dict):
            return None
        if dashboard.get(key) not in (None, ""):
            return dashboard.get(key)
        for value in dashboard.values():
            if isinstance(value, dict):
                found = SignalLifecycleService._dashboard_value(value, key)
                if found not in (None, ""):
                    return found
        return None

    @classmethod
    def _dashboard_action(cls, dashboard: Any) -> str:
        candidates = [
            cls._dashboard_value(dashboard, "decision_type"),
            cls._dashboard_value(dashboard, "operation_advice"),
            cls._dashboard_value(dashboard, "final_action"),
        ]
        texts = [str(candidate or "").strip().lower() for candidate in candidates]
        hard_rejection_tokens = (
            "卖出", "减仓", "退出", "回避", "sell", "reduce", "exit", "reject", "avoid",
        )
        if any(any(token in text for token in hard_rejection_tokens) for text in texts):
            return "reject"
        if any(any(token in text for token in ("加仓", "add")) for text in texts):
            return "add"
        if any(any(token in text for token in ("买入", "开仓", "buy", "open", "entry")) for text in texts):
            return "open"
        return "wait"

    @classmethod
    def _dashboard_sniper_points(cls, dashboard: Any) -> Dict[str, Optional[float]]:
        points = cls._dashboard_value(dashboard, "sniper_points")
        if not isinstance(points, dict):
            points = {}
        return {
            "ideal_buy": _float(points.get("ideal_buy") or cls._dashboard_value(dashboard, "ideal_buy")),
            "secondary_buy": _float(points.get("secondary_buy") or cls._dashboard_value(dashboard, "secondary_buy")),
            "stop_loss": _float(points.get("stop_loss") or cls._dashboard_value(dashboard, "stop_loss")),
        }

    @staticmethod
    def _resolve_price_plan_artifact(
        *,
        result: Any,
        risk_gate_payload: Optional[Dict[str, Any]],
        symbol: Optional[str],
    ) -> Optional[PricePlanArtifact]:
        candidates: List[Any] = []
        if isinstance(risk_gate_payload, dict):
            candidates.extend(
                [
                    risk_gate_payload.get("price_plan"),
                    (risk_gate_payload.get("trade_plan_result") or {}).get("price_plan")
                    if isinstance(risk_gate_payload.get("trade_plan_result"), dict)
                    else None,
                ]
            )
        candidates.extend(
            [
                getattr(result, "price_plan", None),
                getattr(getattr(result, "trade_plan_result", None), "price_plan", None),
            ]
        )
        expected = canonical_stock_code(normalize_stock_code(symbol or "")) or str(symbol or "").upper()
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                artifact = (
                    candidate
                    if isinstance(candidate, PricePlanArtifact)
                    else PricePlanArtifact.model_validate(candidate)
                )
            except Exception:
                continue
            actual = canonical_stock_code(normalize_stock_code(artifact.symbol)) or artifact.symbol.upper()
            if not expected or actual == expected:
                return artifact
        return None

    @staticmethod
    def _select_price_plan_scenario(
        artifact: Optional[PricePlanArtifact],
        *,
        selected_scenario_id: Optional[str] = None,
    ) -> Optional[ZoneScenario]:
        if artifact is None:
            return None
        candidates = [
            item
            for item in artifact.scenarios
            if item.role is not ScenarioRole.OBSERVATION
            and item.price_relation.value in {"above", "inside"}
            and item.stop_loss is not None
            and item.target_1 is not None
        ]
        selected = next(
            (
                item
                for item in candidates
                if selected_scenario_id and item.scenario_id == selected_scenario_id
            ),
            None,
        )
        if selected is not None:
            return selected
        return sorted(
            candidates,
            key=lambda item: (
                0 if item.role is ScenarioRole.PRIMARY else 1,
                item.base_rank,
                item.scenario_id,
            ),
        )[0] if candidates else None

    @staticmethod
    def _resolve_selected_scenario_id(
        *,
        result: Any,
        risk_gate_payload: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        candidates: List[Any] = []
        if isinstance(risk_gate_payload, dict):
            candidates.append(risk_gate_payload.get("trade_plan_result"))
        candidates.append(getattr(result, "trade_plan_result", None))
        for candidate in candidates:
            if candidate is None:
                continue
            if hasattr(candidate, "account_decision"):
                decision = getattr(candidate, "account_decision", None)
                selected = getattr(decision, "selected_scenario_id", None)
            elif isinstance(candidate, dict):
                decision = candidate.get("account_decision")
                selected = (
                    decision.get("selected_scenario_id")
                    if isinstance(decision, dict)
                    else None
                )
            else:
                selected = None
            if selected:
                return str(selected)
        return None

    @staticmethod
    def _resolve_account_decision(
        *,
        result: Any,
        risk_gate_payload: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        candidates: List[Any] = []
        if isinstance(risk_gate_payload, dict):
            candidates.append(risk_gate_payload.get("trade_plan_result"))
        candidates.append(getattr(result, "trade_plan_result", None))
        for candidate in candidates:
            if candidate is None:
                continue
            if hasattr(candidate, "account_decision"):
                decision = getattr(candidate, "account_decision", None)
                if hasattr(decision, "model_dump"):
                    return decision.model_dump(mode="json")
            elif isinstance(candidate, dict) and isinstance(candidate.get("account_decision"), dict):
                return dict(candidate["account_decision"])
        return {}

    @staticmethod
    def _artifact_zones(
        artifact: Optional[PricePlanArtifact],
    ) -> List[Dict[str, Any]]:
        if artifact is None:
            return []
        label_map = {
            ScenarioRole.PRIMARY: "主入场区",
            ScenarioRole.SECONDARY: "次入场区",
            ScenarioRole.OBSERVATION: "观察区",
        }
        return [
            {
                "scenario_id": item.scenario_id,
                "label": label_map[item.role],
                "role": item.role.value,
                "low": float(item.entry_low),
                "high": float(item.entry_high),
                "stop_loss": float(item.stop_loss) if item.stop_loss is not None else None,
                "target_1": float(item.target_1) if item.target_1 is not None else None,
                "target_2": float(item.target_2) if item.target_2 is not None else None,
                "source_families": list(item.source_families),
                "reason_codes": list(item.scenario_reason_codes),
            }
            for item in artifact.scenarios
        ]

    @staticmethod
    def _scenario_points(
        scenario: Optional[ZoneScenario],
    ) -> Dict[str, Optional[float]]:
        if scenario is None:
            return {"ideal_buy": None, "secondary_buy": None, "stop_loss": None}
        return {
            "ideal_buy": float(scenario.entry_high),
            "secondary_buy": float(scenario.entry_low),
            "stop_loss": float(scenario.stop_loss) if scenario.stop_loss is not None else None,
        }

    @classmethod
    def _project_scenario_trade_plan(
        cls,
        *,
        artifact: PricePlanArtifact,
        scenario: ZoneScenario,
        action: str,
    ) -> TradePlan:
        """Project one formal math scenario into Risk Gate's legacy input."""
        zones = cls._artifact_zones(artifact)
        selected_zone = next(
            (item for item in zones if item.get("scenario_id") == scenario.scenario_id),
            None,
        )
        ordered_zones = ([selected_zone] if selected_zone else []) + [
            item for item in zones if item is not selected_zone
        ]
        return TradePlan(
            symbol=artifact.symbol,
            action=action,  # type: ignore[arg-type]
            current_action="wait",
            order_type="manual",
            entry_zone_low=float(scenario.entry_low),
            entry_zone_high=float(scenario.entry_high),
            entry_zones=[
                {
                    "label": str(item.get("label") or "entry"),
                    "low": float(item["low"]),
                    "high": float(item["high"]),
                    "rationale": (
                        f"deterministic scenario {item.get('scenario_id')} "
                        f"({item.get('role')})"
                    ),
                }
                for item in ordered_zones
            ],
            stop_loss_price=float(scenario.stop_loss) if scenario.stop_loss is not None else None,
            take_profit_price=float(scenario.target_1) if scenario.target_1 is not None else None,
            invalidation_conditions=[rule.rule_type for rule in scenario.invalidation_rules],
            execution_window=(
                f"confirmation_deadline={scenario.confirmation_deadline.isoformat()}; "
                f"confirmation_days={scenario.confirmation_valid_trading_days}; "
                f"order_days={scenario.order_valid_trading_days}"
            ),
            notes="Compatibility projection from deterministic PricePlanArtifact.",
            metadata={
                "price_source": "deterministic_price_plan",
                "plan_id": artifact.plan_id,
                "selected_scenario_id": scenario.scenario_id,
                "generation_status": artifact.generation_status.value,
            },
        )

    @staticmethod
    def _price_plan_summary(artifact: PricePlanArtifact) -> Dict[str, Any]:
        primary = next(
            (item for item in artifact.scenarios if item.role is ScenarioRole.PRIMARY),
            artifact.scenarios[0] if artifact.scenarios else None,
        )
        return _with_display_stock_name({
            "plan_id": artifact.plan_id,
            "stock_code": artifact.symbol,
            "market": artifact.market,
            "generation_status": artifact.generation_status.value,
            "revision": artifact.revision,
            "actual_as_of_session": artifact.actual_as_of_session,
            "entry_low": float(primary.entry_low) if primary is not None else None,
            "entry_high": float(primary.entry_high) if primary is not None else None,
            "reason_codes": list(artifact.reason_codes),
            "scenario_count": len(artifact.scenarios),
            "trace_ref": artifact.trace_ref,
        })

    @staticmethod
    def _resolve_agent_trade_plan(
        *,
        result: Any,
        risk_gate_payload: Optional[Dict[str, Any]],
        symbol: Optional[str],
    ) -> Optional[TradePlan]:
        candidates: List[Any] = []
        if isinstance(risk_gate_payload, dict):
            candidates.append(risk_gate_payload.get("trade_plan"))
        candidates.append(getattr(result, "trade_plan", None))
        expected = canonical_stock_code(normalize_stock_code(symbol or "")) or str(symbol or "").upper()
        for candidate in candidates:
            if not isinstance(candidate, (dict, TradePlan)):
                continue
            try:
                plan = candidate if isinstance(candidate, TradePlan) else TradePlan.model_validate(candidate)
            except Exception:
                continue
            actual = canonical_stock_code(normalize_stock_code(plan.symbol)) or plan.symbol.upper()
            if not expected or actual == expected:
                return plan
        return None

    @staticmethod
    def _trade_plan_points(plan: TradePlan) -> Dict[str, Optional[float]]:
        if plan.entry_price is not None:
            return {
                "ideal_buy": float(plan.entry_price),
                "secondary_buy": None,
                "stop_loss": float(plan.stop_loss_price) if plan.stop_loss_price is not None else None,
            }
        if plan.entry_zone_low is None or plan.entry_zone_high is None:
            return {"ideal_buy": None, "secondary_buy": None, "stop_loss": None}
        return {
            "ideal_buy": float(plan.entry_zone_high),
            "secondary_buy": float(plan.entry_zone_low),
            "stop_loss": float(plan.stop_loss_price) if plan.stop_loss_price is not None else None,
        }

    @staticmethod
    def _trade_plan_zones(plan: TradePlan) -> List[Dict[str, Any]]:
        if plan.entry_zones:
            return [zone.model_dump(mode="json", exclude_none=True) for zone in plan.entry_zones]
        if plan.entry_price is not None:
            price = float(plan.entry_price)
            return [{"label": "entry", "low": price, "high": price}]
        if plan.entry_zone_low is None or plan.entry_zone_high is None:
            return []
        return [{
            "label": "entry",
            "low": float(plan.entry_zone_low),
            "high": float(plan.entry_zone_high),
        }]

    @classmethod
    def _dashboard_invalidation_conditions(cls, dashboard: Any) -> List[str]:
        raw = cls._dashboard_value(dashboard, "invalidation_conditions")
        if isinstance(raw, list):
            return [str(value).strip() for value in raw if str(value).strip()]
        if isinstance(raw, str) and raw.strip():
            return [raw.strip()]
        return []

    @staticmethod
    def _context_position(context: Dict[str, Any], symbol: str) -> Optional[Any]:
        agent_context = context.get("agent_user_context")
        positions = getattr(agent_context, "positions", None)
        if positions is None and isinstance(agent_context, dict):
            positions = agent_context.get("positions")
        for position in positions or []:
            candidate = getattr(position, "symbol", None) if not isinstance(position, dict) else position.get("symbol")
            quantity = getattr(position, "quantity", 0) if not isinstance(position, dict) else position.get("quantity", 0)
            if str(candidate or "").upper() == symbol.upper() and float(quantity or 0) > 0:
                return position
        return None

    def _persisted_position(self, symbol: str) -> Optional[PortfolioPosition]:
        with self.repo.db.get_session() as session:
            rows = session.execute(
                select(PortfolioPosition).where(
                    PortfolioPosition.cost_method == "fifo",
                    PortfolioPosition.quantity > 0,
                )
            ).scalars().all()
            for row in rows:
                candidate = canonical_stock_code(normalize_stock_code(row.symbol)) or str(row.symbol).upper()
                expected = canonical_stock_code(normalize_stock_code(symbol)) or symbol.upper()
                if candidate == expected:
                    session.expunge(row)
                    return row
        return None

    @classmethod
    def _context_owner_account(cls, context: Dict[str, Any], symbol: str) -> Tuple[Optional[str], Optional[int]]:
        position = cls._context_position(context, symbol)
        account_id = getattr(position, "account_id", None) if position is not None and not isinstance(position, dict) else (position or {}).get("account_id")
        agent_context = context.get("agent_user_context")
        accounts = getattr(agent_context, "accounts", None)
        if accounts is None and isinstance(agent_context, dict):
            accounts = agent_context.get("accounts")
        for account in accounts or []:
            candidate_id = getattr(account, "account_id", None) if not isinstance(account, dict) else account.get("account_id")
            if account_id is not None and candidate_id != account_id:
                continue
            owner_id = getattr(account, "owner_id", None) if not isinstance(account, dict) else account.get("owner_id")
            return owner_id, candidate_id
        return None, account_id

    @staticmethod
    def _resolve_risk_gate(
        *,
        expected_action: str,
        symbol: str,
        stop_price: Optional[float],
        invalidation: List[str],
        result: Any,
        risk_gate_payload: Optional[Dict[str, Any]],
        trade_plan: Optional[TradePlan] = None,
    ) -> Dict[str, Any]:
        if isinstance(risk_gate_payload, dict) and isinstance(risk_gate_payload.get("risk_gate"), dict):
            return dict(risk_gate_payload["risk_gate"])
        plan = trade_plan or TradePlan(
            symbol=symbol,
            action=expected_action,  # type: ignore[arg-type]
            order_type="manual",
            stop_loss_price=stop_price,
            invalidation_conditions=invalidation,
            metadata={"source": "signal_lifecycle_capture"},
        )
        failed_tools = [
            str(call.get("tool"))
            for call in getattr(result, "tool_calls_log", []) or []
            if isinstance(call, dict) and call.get("success") is False
        ]
        gate = RiskGateEvaluator().evaluate(RiskGateInput(
            plan=plan,
            quote=QuoteState(symbol=symbol),
            data_quality="failed" if failed_tools else "sufficient",
            failed_tools=failed_tools,
        ))
        return gate.model_dump(mode="json")

    @staticmethod
    def _add_trading_days(start: date, count: int, market: str) -> date:
        current = start
        remaining = max(0, int(count))
        while remaining:
            current += timedelta(days=1)
            if is_market_open(market, current):
                remaining -= 1
        return current


__all__ = ["SignalLifecycleService"]
