"""Append-only Jev reviews of a completed Trace, independent of its decision."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.services.jev_evaluation_service import JevEvaluationService, normalize_symbol

logger = logging.getLogger(__name__)


def _record(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def trace_baseline(result: Any, context: dict, risk_gate: dict | None) -> dict:
    user = context.get("agent_user_context")
    if hasattr(user, "model_dump"):
        user = user.model_dump(mode="json")
    user = _record(user)
    debate = _record(getattr(result, "debate", None))
    judge = _record(debate.get("judge_decision")) if debate.get("success") is True else {}
    gate = _record(_record(risk_gate).get("risk_gate"))
    action = gate.get("allowed_action") or judge.get("final_action")
    return {
        "action": action, "source": "risk_gate" if gate.get("allowed_action") else "judge" if action else "unavailable",
        "judge_action": judge.get("final_action"),
        "summary": str(judge.get("decision_summary") or judge.get("reason") or "")[:2000],
        "reasons": [str(value)[:500] for value in gate.get("blocked_reasons", [])][:8],
        "trading_horizon": _record(user.get("investor")).get("trading_horizon"),
    }


def compare_trace_review(evaluation: dict, baseline: dict, *, shared_snapshot: bool) -> dict:
    action = baseline.get("action")
    reason = None
    if evaluation.get("status") != "ok":
        reason = "review_unavailable"
    elif not shared_snapshot:
        reason = "legacy_snapshot_unverified"
    elif not action:
        reason = "missing_agent_action"
    elif baseline.get("trading_horizon") not in {"short_term", "swing"}:
        reason = "different_or_unknown_horizon"
    rows = []
    for horizon in (1, 3, 5):
        answers = evaluation.get("answers") or {}
        entry = _record(answers.get(f"entry_{horizon}")).get("choice")
        direction = _record(answers.get(f"direction_{horizon}")).get("choice")
        risk = _record(answers.get(f"risk_{horizon}")).get("choice")
        insufficient = direction in {None, "insufficient"} or risk in {None, "insufficient"}
        flags = []
        if not reason and not insufficient:
            if action in {"open", "add"}:
                if entry in {"wait", "avoid"}:
                    flags.append("entry_caution")
                if direction == "bearish" or risk == "high":
                    flags.append("downside_conflict")
            elif action == "hold" and risk == "high":
                flags.append("holding_risk_conflict")
            elif action in {"reduce", "take_profit", "stop_loss", "exit"} and direction == "bullish":
                flags.append("direction_vs_reduction")
        rows.append({"horizon": horizon, "status": "not_comparable" if reason or insufficient else
                     "divergent" if flags else "no_direct_conflict", "flags": flags,
                     "reason": reason or ("insufficient_evidence" if insufficient else None)})
    return {"status": "not_comparable" if all(row["status"] == "not_comparable" for row in rows)
            else "divergent" if any(row["flags"] for row in rows)
            else "no_direct_conflict", "reason": reason, "rows": rows}


class JevTraceService:
    def __init__(self, evaluation_service=None):
        self.evaluator = evaluation_service or JevEvaluationService()

    @staticmethod
    def _write(path: Path, payload: dict) -> None:
        # Polling readers must see either the pending or the complete JSON document.
        temporary = path / f"jev-{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            temporary.replace(path / "jev.json")
        finally:
            temporary.unlink(missing_ok=True)

    def prepare(self, *, path: Path, session_id: str, symbol: str, result: Any,
                context: dict, risk_gate: dict | None, histories: dict) -> dict:
        review = {"status": "skipped", "reason": "single_stock_only", "shadow_mode": True,
                  "capture_id": uuid.uuid4().hex, "session_id": session_id,
                  "created_at": datetime.now(timezone.utc).isoformat(), "shared_snapshot": True,
                  "baseline": trace_baseline(result, context, risk_gate), "evaluation": None}
        if not self.evaluator.config.jev_enabled or not self.evaluator.config.jev_api_key:
            review.update(status="disabled", reason="not_configured")
        else:
            snapshot = histories.get(normalize_symbol(symbol)) if symbol else None
            prepared = self.evaluator.prepare_agent_result(
                stock_code=symbol, session_id=session_id, result=result, context=context,
                risk_gate_payload=risk_gate, daily_snapshot=snapshot or (None, "trace_history_unavailable"),
            )
            if prepared is not None:
                review.update(status=prepared["status"], reason=prepared.get("reason"), evaluation=prepared)
                if snapshot is None:
                    review.update(reason="trace_history_unavailable")
                if review["status"] == "pending":
                    review["expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        self._write(path, review)
        return review

    def finish(self, path: Path, prepared_review: dict) -> None:
        review = json.loads(json.dumps(prepared_review))
        try:
            current = json.loads((path / "jev.json").read_text(encoding="utf-8"))
            if current.get("capture_id") != review["capture_id"]:
                return
            evaluation = self.evaluator.evaluate_prepared(review["evaluation"])
            review.update(status=evaluation["status"], reason=evaluation.get("reason"), evaluation=evaluation)
            review["comparison"] = compare_trace_review(evaluation, review["baseline"], shared_snapshot=True)
        except Exception:
            logger.warning("Jev Trace review failed for %s", review["session_id"])
            review.update(status="error", reason="shadow_capture_error")
        review["completed_at"] = datetime.now(timezone.utc).isoformat()
        try:
            current = json.loads((path / "jev.json").read_text(encoding="utf-8"))
            if current.get("capture_id") != review["capture_id"]:
                return
            self._write(path, review)
            with (path / "events.ndjson").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "jev_review_done", "status": review["status"],
                                         "capture_id": review["capture_id"], "session_id": review["session_id"]}) + "\n")
        except Exception:
            logger.warning("Jev Trace review persistence failed for %s", review["session_id"])

    def read(self, path: Path, session_id: str, *, outcomes: bool = False) -> dict:
        try:
            review = json.loads((path / "jev.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            records = self.evaluator.repo.list_recent(source="agent", source_ref=session_id, limit=1)
            evaluation = records[0] if records else None
            review = {"status": evaluation["status"] if evaluation else "not_recorded", "shadow_mode": True,
                      "session_id": session_id, "shared_snapshot": False, "baseline": {}, "evaluation": evaluation}
            if evaluation:
                review["comparison"] = compare_trace_review(evaluation, {}, shared_snapshot=False)
        if review.get("session_id") != session_id:
            return {"status": "unavailable", "reason": "trace_mismatch", "shadow_mode": True}
        if review["status"] == "pending" and datetime.fromisoformat(review["expires_at"]) < datetime.now(timezone.utc):
            review.update(status="interrupted", reason="review_expired")
        if outcomes and review.get("evaluation"):
            review["evaluation"]["outcomes"] = self.evaluator.outcomes(review["evaluation"])
        return review
