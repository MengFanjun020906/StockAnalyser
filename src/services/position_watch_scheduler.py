# -*- coding: utf-8 -*-
"""Market-aware L0/L1 monitoring for persisted portfolio positions."""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.agent.risk_gate import QuoteState, RiskGateEvaluator, RiskGateInput
from src.config import get_config
from src.core.trading_calendar import MARKET_TIMEZONE, get_effective_trading_date, get_market_now, is_market_open
from src.repositories.signal_lifecycle_repo import SignalLifecycleRepository
from src.schemas.agent_signal import TradePlan
from src.schemas.agent_context import PositionContext
from src.services.portfolio_service import PortfolioService
from src.services.signal_market_data_service import SignalMarketDataService
from src.storage import PortfolioPositionLot
from sqlalchemy import func, select


logger = logging.getLogger(__name__)
MARKET_RUN_TIME = {"cn": time(18, 0), "hk": time(18, 0), "us": time(16, 30)}


class PositionWatchSchedulerService:
    """Poll market windows and maintain one watch per active holding cycle."""

    def __init__(
        self,
        *,
        repository: Optional[SignalLifecycleRepository] = None,
        portfolio_service: Optional[PortfolioService] = None,
        market_data: Optional[SignalMarketDataService] = None,
        l1_runner: Optional[Callable[..., Dict[str, Any]]] = None,
    ) -> None:
        self.repo = repository or SignalLifecycleRepository()
        self.portfolio = portfolio_service or PortfolioService()
        self.market_data = market_data or SignalMarketDataService(self.repo.db)
        self._l1_runner = l1_runner or self._run_l1_agent

    def poll_due_markets(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Update heartbeats and run each due market at most once per session."""
        reference = now or datetime.now(timezone.utc)
        result = {"checked_at": reference.isoformat(), "markets": {}}
        states = {item["market"]: item for item in self.repo.list_scheduler_states()}
        for market in ("cn", "hk", "us"):
            market_now = get_market_now(market, current_time=reference)
            run_date = get_effective_trading_date(market, current_time=reference)
            due = (
                is_market_open(market, market_now.date())
                and market_now.time().replace(tzinfo=None) >= MARKET_RUN_TIME[market]
                and run_date == market_now.date()
            )
            previous = states.get(market) or {}
            already_ran = previous.get("last_trading_date") == run_date and previous.get("last_success_at") is not None
            next_run = self._next_run_utc(market, market_now)
            if not due or already_ran:
                self.repo.update_scheduler_state(
                    market,
                    status="idle",
                    heartbeat_at=self._utc_naive(reference),
                    next_run_at=next_run,
                )
                result["markets"][market] = {"status": "idle", "due": due, "already_ran": already_ran}
                continue

            started = self._utc_naive(reference)
            self.repo.update_scheduler_state(
                market,
                status="running",
                heartbeat_at=started,
                last_started_at=started,
                next_run_at=next_run,
                error=None,
            )
            try:
                summary = self.scan_market(market, as_of=run_date)
                finished = datetime.utcnow()
                self.repo.update_scheduler_state(
                    market,
                    status="success",
                    heartbeat_at=finished,
                    last_success_at=finished,
                    last_trading_date=run_date,
                    next_run_at=self._next_run_utc(market, market_now + timedelta(minutes=1)),
                    watch_count=summary["watch_count"],
                    l1_count=summary["l1_count"],
                    error=None,
                )
                result["markets"][market] = summary
            except Exception as exc:
                logger.exception("Position watch scan failed for %s: %s", market, exc)
                failed_at = datetime.utcnow()
                self.repo.update_scheduler_state(
                    market,
                    status="failed",
                    heartbeat_at=failed_at,
                    last_started_at=started,
                    next_run_at=next_run,
                    error=str(exc),
                )
                result["markets"][market] = {"status": "failed", "error": str(exc)}
        return result

    def scan_market(self, market: str, *, as_of: date) -> Dict[str, Any]:
        snapshot = self.portfolio.get_portfolio_snapshot(as_of=as_of, cost_method="fifo")
        positions = self._positions_for_market(snapshot, market, as_of=as_of)
        active_keys = {(int(item["account_id"]), item["stock_code"], item["market"]) for item in positions}
        l1_count = 0
        changed_count = 0
        for position in positions:
            previous = self._active_watch(position)
            evaluation = self._evaluate_l0(position, previous=previous, as_of=as_of)
            l1 = None
            if evaluation["l1_required"]:
                l1_count += 1
                l1 = self._l1_runner(
                    position=position,
                    snapshot=snapshot,
                    evaluation=evaluation,
                    as_of=as_of,
                )
                evaluation = self._apply_l1_review(position, evaluation=evaluation, l1=l1)
            previous_action = previous.get("current_action") if previous else None
            previous_risk = previous.get("risk_level") if previous else None
            position_changed = bool(previous and (
                abs(float(previous.get("quantity") or 0) - float(position.get("quantity") or 0)) > 1e-9
                or abs(float(previous.get("average_cost") or 0) - float(position.get("average_cost") or 0)) > 1e-9
            ))
            if previous_action != evaluation["action"] or previous_risk != evaluation["risk_level"]:
                changed_count += 1
            event_type = "position_changed" if position_changed else "position_risk_changed" if previous and (
                previous_action != evaluation["action"] or previous_risk != evaluation["risk_level"]
            ) else "position_checked"
            payload = {
                "market": market,
                "trading_date": as_of.isoformat(),
                "quantity": position["quantity"],
                "average_cost": position["average_cost"],
                "latest_close": evaluation.get("latest_close"),
                "score": evaluation.get("score"),
                "exit_warning": evaluation.get("exit_warning"),
                "reasons": evaluation.get("reasons"),
                "l1_required": evaluation["l1_required"],
                "l1": l1,
                "risk_gate": evaluation.get("risk_gate"),
                "data_window": evaluation.get("data_window"),
            }
            if get_config().jev_enabled:
                try:
                    from src.services.jev_evaluation_service import JevEvaluationService

                    payload["jev_evaluation"] = JevEvaluationService().evaluate(
                        position["stock_code"], as_of=as_of, source="position_watch",
                        source_ref=f"account:{position['account_id']}", position=position,
                    )
                except Exception:
                    logger.warning("Jev position shadow unavailable for %s", position["stock_code"])
                    payload["jev_evaluation"] = {"status": "error", "reason": "shadow_capture_error"}
            should_notify = self._should_notify(previous, position=position, current_action=evaluation["action"], current_risk=evaluation["risk_level"])
            notification = self._notification_payload(
                position=position,
                evaluation=evaluation,
                watch_version=(int(previous.get("version") or 0) + 1) if previous else 1,
            ) if should_notify else None
            watch = self.repo.upsert_position_watch(
                position=position,
                as_of=datetime.combine(as_of, MARKET_RUN_TIME[market]),
                risk_level=evaluation["risk_level"],
                current_action=evaluation["action"],
                score=evaluation.get("score"),
                exit_warning_level=evaluation.get("exit_warning_level", 0),
                event_type=event_type,
                event_payload=payload,
                dedupe_key=f"position-watch:{market}:{as_of.isoformat()}:{position['account_id']}:{position['stock_code']}",
                needs_review=bool(evaluation.get("needs_review")),
                notification=notification,
            )

        for watch in self.repo.list_position_watches(statuses=["active", "needs_review"], limit=1000):
            key = (int(watch["account_id"]), watch["stock_code"], watch["market"])
            if watch["market"] != market or key in active_keys:
                continue
            self.repo.close_position_watch(
                watch["watch_id"],
                as_of=datetime.combine(as_of, MARKET_RUN_TIME[market]),
                dedupe_key=f"position-watch:{market}:{as_of.isoformat()}:{watch['watch_id']}:closed",
                notification={
                    "dedupe_key": f"{watch['watch_id']}:{int(watch.get('version') or 0) + 1}:exit",
                    "action": "exit",
                    "payload": {
                        "stock_code": watch["stock_code"],
                        "stock_name": watch.get("stock_name"),
                        "account_id": watch["account_id"],
                        "action": "exit",
                        "risk_level": watch.get("risk_level"),
                        "reasons": ["持仓数量归零，监控周期结束"],
                    },
                },
            )

        self._dispatch_notifications()
        return {
            "status": "success",
            "market": market,
            "trading_date": as_of.isoformat(),
            "watch_count": len(positions),
            "l1_count": l1_count,
            "changed_count": changed_count,
        }

    def _evaluate_l0(
        self,
        position: Dict[str, Any],
        *,
        previous: Optional[Dict[str, Any]],
        as_of: date,
    ) -> Dict[str, Any]:
        chart = self.market_data.load_chart(
            position["stock_code"],
            market=position["market"],
            account_id=position["account_id"],
            refresh_tail=True,
            as_of=as_of,
            holding=True,
        )
        composite_payload = chart.get("composite") if isinstance(chart.get("composite"), dict) else {}
        verdict = composite_payload.get("composite_verdict") if isinstance(composite_payload.get("composite_verdict"), dict) else {}
        score = verdict.get("score")
        warning = verdict.get("exit_warning") if isinstance(verdict.get("exit_warning"), dict) else {}
        warning_level = int(warning.get("level") or 0)
        bars = chart.get("bars") or []
        latest_close = bars[-1].get("close") if bars else None
        stop_price = (previous or {}).get("stop_price")
        reasons: List[str] = []
        action = "hold"
        risk_level = "low"

        if chart.get("status") != "ok" or verdict.get("verdict_status") != "evaluable":
            risk_level = "unknown"
            reasons.append("deterministic_score_not_evaluable")
        if stop_price is not None and latest_close is not None and float(latest_close) <= float(stop_price):
            action = "exit"
            risk_level = "critical"
            reasons.append("daily_close_below_frozen_stop")
        elif warning_level >= 3:
            action = "reduce"
            risk_level = "high"
            reasons.append("exit_warning_level_3")
        elif warning_level >= 1:
            risk_level = "medium"
            reasons.append(f"exit_warning_level_{warning_level}")

        previous_score = (previous or {}).get("latest_score")
        if score is not None and previous_score is not None and int(previous_score) - int(score) >= 5:
            if risk_level == "low":
                risk_level = "medium"
            reasons.append("composite_score_drop_ge_5")

        l1_required = bool(reasons) and action == "hold"
        risk_gate = self._risk_gate(
            position=position,
            action=action,
            stop_price=stop_price,
            reasons=reasons,
            latest_close=latest_close,
        )
        if risk_gate.get("allowed_action") == "manual_review":
            l1_required = True
        return {
            "action": action,
            "risk_level": risk_level,
            "score": int(score) if score is not None else None,
            "exit_warning": warning,
            "exit_warning_level": warning_level,
            "latest_close": latest_close,
            "stop_price": stop_price,
            "reasons": reasons,
            "l1_required": l1_required,
            "risk_gate": risk_gate,
            "data_window": chart.get("data_window"),
            "needs_review": False,
        }

    def _apply_l1_review(
        self,
        position: Dict[str, Any],
        *,
        evaluation: Dict[str, Any],
        l1: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        updated = dict(evaluation)
        dashboard = l1.get("dashboard") if isinstance(l1, dict) else None
        if not isinstance(l1, dict) or l1.get("status") != "ok" or not isinstance(dashboard, dict):
            updated["needs_review"] = True
            updated["reasons"] = [*updated.get("reasons", []), "l1_review_unavailable"]
            return updated

        desired = self._l1_action(dashboard)
        if desired not in {"reduce", "exit"}:
            updated["needs_review"] = False
            return updated
        gate = self._risk_gate(
            position=position,
            action=desired,
            stop_price=updated.get("stop_price"),
            reasons=[*updated.get("reasons", []), f"l1_recommends_{desired}"],
            latest_close=updated.get("latest_close"),
        )
        updated["risk_gate"] = gate
        if gate.get("allowed_action") in {"reduce", "sell"}:
            updated["action"] = "exit" if gate.get("allowed_action") == "sell" else "reduce"
            updated["risk_level"] = "critical" if updated["action"] == "exit" else "high"
            updated["needs_review"] = False
        else:
            updated["needs_review"] = True
            updated["reasons"] = [*updated.get("reasons", []), "l1_action_requires_manual_review"]
        return updated

    @classmethod
    def _l1_action(cls, payload: Dict[str, Any]) -> str:
        for key, value in payload.items():
            if key in {"decision_type", "operation_advice", "final_action", "action"}:
                candidates = value.values() if isinstance(value, dict) else [value]
                for candidate in candidates:
                    text = str(candidate or "").strip().lower()
                    if any(token in text for token in ("卖出", "清仓", "sell", "exit")):
                        return "exit"
                    if any(token in text for token in ("减仓", "reduce", "trim")):
                        return "reduce"
            if isinstance(value, dict):
                nested = cls._l1_action(value)
                if nested != "hold":
                    return nested
        return "hold"

    def _risk_gate(
        self,
        *,
        position: Dict[str, Any],
        action: str,
        stop_price: Optional[float],
        reasons: List[str],
        latest_close: Optional[float],
    ) -> Dict[str, Any]:
        plan_action = "sell" if action == "exit" else action
        plan = TradePlan(
            symbol=position["stock_code"],
            action=plan_action,  # type: ignore[arg-type]
            order_type="manual",
            stop_loss_price=stop_price,
            invalidation_conditions=reasons,
            metadata={"source": "position_watch_l0"},
        )
        gate = RiskGateEvaluator().evaluate(RiskGateInput(
            plan=plan,
            quote=QuoteState(
                symbol=position["stock_code"],
                last_price=latest_close,
                market=position["market"],
            ),
            position=PositionContext(
                symbol=position["stock_code"],
                account_id=int(position["account_id"]),
                quantity=float(position.get("quantity") or 0),
                avg_cost=float(position.get("average_cost") or 0),
                holding_days=position.get("holding_days"),
            ),
            data_quality="insufficient" if "deterministic_score_not_evaluable" in reasons else "sufficient",
        ))
        return gate.model_dump(mode="json")

    def _run_l1_agent(
        self,
        *,
        position: Dict[str, Any],
        snapshot: Dict[str, Any],
        evaluation: Dict[str, Any],
        as_of: date,
    ) -> Dict[str, Any]:
        config = get_config()
        if not config.is_agent_available():
            return {"status": "unavailable", "reason": "agent_not_configured"}
        from src.agent.context_builder import build_agent_user_context_from_portfolio_snapshot
        from src.agent.conversation import conversation_manager
        from src.agent.factory import build_agent_executor
        from src.storage import get_db

        context_model = build_agent_user_context_from_portfolio_snapshot(
            snapshot,
            primary_symbol=position["stock_code"],
            target_symbols=[position["stock_code"]],
            user_prompt="持仓风险异常复核",
            analysis_mode="planning_execute",
            report_language=getattr(config, "report_language", "zh"),
        )
        context_model.report.intent = "position_review"
        session_id = f"position-watch-{position['account_id']}-{position['stock_code']}-{as_of:%Y%m%d}-{uuid.uuid4().hex[:6]}"
        prompt = (
            f"复核持仓 {position['stock_code']}，当前确定性扫描异常："
            f"{', '.join(evaluation.get('reasons') or [])}。"
            "请运行技术面、消息面、资金面、基本面四维分析，只给持有、减仓或退出建议，不提出自动下单。"
        )
        try:
            result = build_agent_executor(config).chat(
                message=prompt,
                session_id=session_id,
                context={
                    "stock_code": position["stock_code"],
                    "stock_name": position.get("stock_name"),
                    "agent_user_context": context_model,
                    "as_of": as_of.isoformat(),
                    "position_watch": True,
                },
            )
            return {
                "status": "ok" if result.success else "failed",
                "session_id": session_id,
                "content": (result.content or "")[:4000],
                "dashboard": result.dashboard,
                "error": result.error,
                "provider": result.provider,
                "model": result.model,
            }
        except Exception as exc:
            logger.warning("Position watch L1 failed for %s: %s", position["stock_code"], exc, exc_info=True)
            return {"status": "failed", "session_id": session_id, "error": str(exc)}
        finally:
            try:
                conversation_manager.clear(session_id)
                get_db().delete_conversation_session(session_id)
            except Exception:
                pass

    def _positions_for_market(
        self,
        snapshot: Dict[str, Any],
        market: str,
        *,
        as_of: date,
    ) -> List[Dict[str, Any]]:
        positions: List[Dict[str, Any]] = []
        for account in snapshot.get("accounts") or []:
            if not isinstance(account, dict):
                continue
            for item in account.get("positions") or []:
                if not isinstance(item, dict) or float(item.get("quantity") or 0) <= 0:
                    continue
                item_market = str(item.get("market") or account.get("market") or "cn").lower()
                if item_market != market:
                    continue
                positions.append({
                    "account_id": int(account["account_id"]),
                    "owner_id": account.get("owner_id"),
                    "stock_code": str(item.get("symbol") or "").strip().upper(),
                    "stock_name": item.get("stock_name") or item.get("name"),
                    "market": item_market,
                    "quantity": float(item.get("quantity") or 0),
                    "average_cost": float(item.get("avg_cost") or 0),
                    "holding_days": self._holding_days(
                        account_id=int(account["account_id"]),
                        stock_code=str(item.get("symbol") or "").strip().upper(),
                        market=item_market,
                        as_of=as_of,
                    ),
                    "source_position_updated_at": datetime.now(),
                    "metadata": {
                        "currency": item.get("currency"),
                        "cost_method": account.get("cost_method") or snapshot.get("cost_method"),
                        "price_source": item.get("price_source"),
                        "price_stale": item.get("price_stale"),
                    },
                })
        return positions

    def _holding_days(
        self,
        *,
        account_id: int,
        stock_code: str,
        market: str,
        as_of: date,
    ) -> Optional[int]:
        with self.repo.db.get_session() as session:
            opened = session.execute(
                select(func.min(PortfolioPositionLot.open_date)).where(
                    PortfolioPositionLot.account_id == account_id,
                    PortfolioPositionLot.symbol == stock_code,
                    PortfolioPositionLot.market == market,
                    PortfolioPositionLot.cost_method == "fifo",
                    PortfolioPositionLot.remaining_quantity > 0,
                )
            ).scalar_one_or_none()
        return max(0, (as_of - opened).days) if opened is not None else None

    def _active_watch(self, position: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for watch in self.repo.list_position_watches(
            account_id=int(position["account_id"]),
            statuses=["active", "needs_review"],
            limit=500,
        ):
            if watch["stock_code"] == position["stock_code"] and watch["market"] == position["market"]:
                return watch
        return None

    @staticmethod
    def _should_notify(
        previous: Optional[Dict[str, Any]],
        *,
        position: Dict[str, Any],
        current_action: str,
        current_risk: str,
    ) -> bool:
        if previous is None:
            return current_action in {"reduce", "exit"} or current_risk in {"high", "critical"}
        return (
            previous.get("current_action") != current_action
            or previous.get("risk_level") != current_risk
            or abs(float(previous.get("quantity") or 0) - float(position.get("quantity") or 0)) > 1e-9
            or abs(float(previous.get("average_cost") or 0) - float(position.get("average_cost") or 0)) > 1e-9
        )

    @staticmethod
    def _notification_payload(
        *,
        position: Dict[str, Any],
        evaluation: Dict[str, Any],
        watch_version: int,
    ) -> Dict[str, Any]:
        action = str(evaluation.get("action") or "hold")
        dedupe_key = f"{position['account_id']}:{position['stock_code']}:{watch_version}:{action}"
        return {
            "dedupe_key": dedupe_key,
            "action": action,
            "payload": {
                "stock_code": position["stock_code"],
                "stock_name": position.get("stock_name"),
                "account_id": position["account_id"],
                "action": action,
                "risk_level": evaluation.get("risk_level"),
                "reasons": evaluation.get("reasons") or ["状态变化"],
            },
        }

    def _dispatch_notifications(self) -> None:
        try:
            pending = self.repo.claim_notification_batch(limit=20)
        except Exception as exc:
            logger.warning("Signal notification outbox claim failed: %s", exc)
            return
        if not isinstance(pending, list):
            return
        try:
            from src.notification import NotificationService
            notifier = NotificationService()
        except Exception as exc:
            logger.warning("Signal notification service unavailable: %s", exc)
            for item in pending:
                if isinstance(item, dict) and item.get("notification_id"):
                    self.repo.complete_notification(item["notification_id"], sent=False, error=str(exc))
            return
        for item in pending:
            payload = item.get("payload") if isinstance(item, dict) else {}
            payload = payload if isinstance(payload, dict) else {}
            try:
                content = (
                    f"持仓信号更新 {payload.get('stock_name') or payload.get('stock_code')}\n"
                    f"动作：{payload.get('action')}\n"
                    f"风险：{payload.get('risk_level')}\n"
                    f"原因：{', '.join(payload.get('reasons') or ['状态变化'])}"
                )
                sent = bool(notifier.send(content))
                self.repo.complete_notification(item["notification_id"], sent=sent, error=None if sent else "no_channel_succeeded")
            except Exception as exc:
                logger.warning("Position watch notification failed: %s", exc)
                self.repo.complete_notification(item["notification_id"], sent=False, error=str(exc))

    @staticmethod
    def _utc_naive(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    @staticmethod
    def _next_run_utc(market: str, market_now: datetime) -> datetime:
        from zoneinfo import ZoneInfo

        candidate_date = market_now.date()
        if market_now.time().replace(tzinfo=None) >= MARKET_RUN_TIME[market]:
            candidate_date += timedelta(days=1)
        while not is_market_open(market, candidate_date):
            candidate_date += timedelta(days=1)
        local = datetime.combine(candidate_date, MARKET_RUN_TIME[market], tzinfo=ZoneInfo(MARKET_TIMEZONE[market]))
        return local.astimezone(timezone.utc).replace(tzinfo=None)


def build_position_watch_background_tasks(config: Any) -> List[Dict[str, Any]]:
    """Return the schedule-worker registration for market-aware watch polling."""
    if not bool(getattr(config, "position_watch_enabled", True)):
        return []
    service = PositionWatchSchedulerService()
    return [{
        "task": service.poll_due_markets,
        "interval_seconds": 300,
        "run_immediately": True,
        "name": "position_watch_scheduler",
    }]


__all__ = ["PositionWatchSchedulerService", "build_position_watch_background_tasks"]
