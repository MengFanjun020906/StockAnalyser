"""Bar disposition ledger for backtests.

Records one entry per discarded or unusable bar/session so that a run can
answer "how many bars were dropped, and why".  The ledger is observational
only: it never changes which bars the backtest uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Iterable, Mapping

LEDGER_SCHEMA_VERSION = "backtest-bar-drops.v1"
DEFAULT_SAMPLE_LIMIT = 500
# Only the first N entries are materialised; aggregates still cover every bar.
DEFAULT_MAX_ENTRIES = DEFAULT_SAMPLE_LIMIT


class BarDropCategory(StrEnum):
    DATA_ISSUE = "data_issue"
    RULE_EXCLUSION = "rule_exclusion"


class BarDropReason(StrEnum):
    # --- data issues: usually worth alerting ---
    INVALID_SYMBOL = "invalid_symbol"
    MISSING_PRICE = "missing_price"
    NON_POSITIVE_PRICE = "non_positive_price"
    IMPOSSIBLE_OHLC = "impossible_ohlc"
    DUPLICATE_BAR = "duplicate_bar"
    MISSING_BAR = "missing_bar"
    PARTIAL_SESSION = "partial_session"
    NO_BAR_FOR_ORDER = "no_bar_for_order"
    STALE_PRICE = "stale_price"
    CORPORATE_ACTION_UNAPPLIED = "corporate_action_unapplied"
    # --- rule exclusions: expected market mechanics / configuration ---
    ZERO_VOLUME_SUSPENDED = "zero_volume_suspended"
    RULE_SUSPENDED = "rule_suspended"
    LIMIT_UP_LOCKED = "limit_up_locked"
    LIMIT_DOWN_LOCKED = "limit_down_locked"
    PRE_LISTING = "pre_listing"
    UNSUPPORTED_INSTRUMENT = "unsupported_instrument"
    CAPACITY_ZERO = "capacity_zero"


REASON_CATEGORY: dict[BarDropReason, BarDropCategory] = {
    BarDropReason.INVALID_SYMBOL: BarDropCategory.DATA_ISSUE,
    BarDropReason.MISSING_PRICE: BarDropCategory.DATA_ISSUE,
    BarDropReason.NON_POSITIVE_PRICE: BarDropCategory.DATA_ISSUE,
    BarDropReason.IMPOSSIBLE_OHLC: BarDropCategory.DATA_ISSUE,
    BarDropReason.DUPLICATE_BAR: BarDropCategory.DATA_ISSUE,
    BarDropReason.MISSING_BAR: BarDropCategory.DATA_ISSUE,
    BarDropReason.PARTIAL_SESSION: BarDropCategory.DATA_ISSUE,
    BarDropReason.NO_BAR_FOR_ORDER: BarDropCategory.DATA_ISSUE,
    BarDropReason.STALE_PRICE: BarDropCategory.DATA_ISSUE,
    BarDropReason.CORPORATE_ACTION_UNAPPLIED: BarDropCategory.DATA_ISSUE,
    BarDropReason.ZERO_VOLUME_SUSPENDED: BarDropCategory.RULE_EXCLUSION,
    BarDropReason.RULE_SUSPENDED: BarDropCategory.RULE_EXCLUSION,
    BarDropReason.LIMIT_UP_LOCKED: BarDropCategory.RULE_EXCLUSION,
    BarDropReason.LIMIT_DOWN_LOCKED: BarDropCategory.RULE_EXCLUSION,
    BarDropReason.PRE_LISTING: BarDropCategory.RULE_EXCLUSION,
    BarDropReason.UNSUPPORTED_INSTRUMENT: BarDropCategory.RULE_EXCLUSION,
    BarDropReason.CAPACITY_ZERO: BarDropCategory.RULE_EXCLUSION,
}

# Reasons whose mere presence indicates a data pipeline problem.
ALERT_REASONS: frozenset[BarDropReason] = frozenset(
    {
        BarDropReason.INVALID_SYMBOL,
        BarDropReason.MISSING_PRICE,
        BarDropReason.NON_POSITIVE_PRICE,
        BarDropReason.IMPOSSIBLE_OHLC,
        BarDropReason.MISSING_BAR,
        BarDropReason.NO_BAR_FOR_ORDER,
        BarDropReason.STALE_PRICE,
    }
)

REASON_DESCRIPTION: dict[BarDropReason, str] = {
    BarDropReason.INVALID_SYMBOL: "Symbol could not be normalized to a supported CN code.",
    BarDropReason.MISSING_PRICE: "Price field missing/unparseable while loading a bar.",
    BarDropReason.NON_POSITIVE_PRICE: "OHLC contains a non-positive price.",
    BarDropReason.IMPOSSIBLE_OHLC: "OHLC relationships are impossible (high/low inconsistent).",
    BarDropReason.DUPLICATE_BAR: "Duplicate (symbol, session/time) bar; later row replaced the earlier one.",
    BarDropReason.MISSING_BAR: "No bar at all for an expected symbol/session.",
    BarDropReason.PARTIAL_SESSION: "Fewer minute bars than expected for the session.",
    BarDropReason.NO_BAR_FOR_ORDER: "An order existed but the symbol had no bar for that session.",
    BarDropReason.STALE_PRICE: "No last price available for end-of-session valuation.",
    BarDropReason.CORPORATE_ACTION_UNAPPLIED: "Corporate action could not be fully applied (e.g. fractional shares).",
    BarDropReason.ZERO_VOLUME_SUSPENDED: "Bar has zero volume; treated as suspended by execution.",
    BarDropReason.RULE_SUSPENDED: "Rule snapshot marks the symbol suspended for the session.",
    BarDropReason.LIMIT_UP_LOCKED: "One-price limit-up bar; buy orders cannot fill.",
    BarDropReason.LIMIT_DOWN_LOCKED: "One-price limit-down bar; sell orders cannot fill.",
    BarDropReason.PRE_LISTING: "Session is before the instrument's first visible bar.",
    BarDropReason.UNSUPPORTED_INSTRUMENT: "Instrument type is not supported by the backtest.",
    BarDropReason.CAPACITY_ZERO: "Participation/liquidity limits reduced fill capacity to zero.",
}

# Higher priority wins when several layers explain the same bar.
REASON_PRIORITY: dict[BarDropReason, int] = {
    BarDropReason.INVALID_SYMBOL: 90,
    BarDropReason.MISSING_PRICE: 85,
    BarDropReason.NON_POSITIVE_PRICE: 80,
    BarDropReason.IMPOSSIBLE_OHLC: 75,
    BarDropReason.DUPLICATE_BAR: 70,
    BarDropReason.NO_BAR_FOR_ORDER: 65,
    BarDropReason.ZERO_VOLUME_SUSPENDED: 60,
    BarDropReason.RULE_SUSPENDED: 55,
    BarDropReason.LIMIT_UP_LOCKED: 50,
    BarDropReason.LIMIT_DOWN_LOCKED: 45,
    BarDropReason.PARTIAL_SESSION: 40,
    BarDropReason.PRE_LISTING: 35,
    BarDropReason.MISSING_BAR: 30,
    BarDropReason.STALE_PRICE: 20,
    BarDropReason.CORPORATE_ACTION_UNAPPLIED: 15,
    BarDropReason.UNSUPPORTED_INSTRUMENT: 10,
    BarDropReason.CAPACITY_ZERO: 5,
}


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass
class BarDrop:
    symbol: str
    session: date | str
    timeframe: str
    reason: BarDropReason
    category: BarDropCategory
    stage: str
    source: str = ""
    slot: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    occurrences: int = 1

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.symbol, str(self.session), self.timeframe, self.slot)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "session": str(self.session),
            "timeframe": self.timeframe,
            "slot": self.slot,
            "reason": self.reason.value,
            "category": self.category.value,
            "stage": self.stage,
            "source": self.source,
            "occurrences": self.occurrences,
            "detail": _json_safe(self.detail),
        }


class BarDropLedger:
    """Collects bar-level drop reasons with de-duplication and aggregation."""

    def __init__(self, *, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self._entries: dict[tuple[str, str, str, str], BarDrop] = {}
        self._seen: set[tuple[str, str, str, str]] = set()
        self._max_entries = max_entries
        self._dropped_entries = 0
        self._total_events = 0
        self._by_reason: dict[str, int] = {}
        self._by_category: dict[str, int] = {}
        self._by_session: dict[str, int] = {}
        self._by_symbol: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._seen)

    @property
    def is_full(self) -> bool:
        return len(self._entries) >= self._max_entries

    @staticmethod
    def _bump(counter: dict[str, int], key: str, delta: int) -> None:
        value = counter.get(key, 0) + delta
        if value:
            counter[key] = value
        else:
            counter.pop(key, None)

    def record(
        self,
        *,
        symbol: str,
        session: date | str,
        timeframe: str,
        reason: BarDropReason | str,
        stage: str,
        source: str = "",
        slot: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        reason_enum = BarDropReason(reason)
        category = REASON_CATEGORY[reason_enum]
        key = (str(symbol), str(session), str(timeframe), str(slot))
        self._total_events += 1
        if key in self._seen:
            entry = self._entries.get(key)
            if entry is not None:
                entry.occurrences += 1
                if REASON_PRIORITY[reason_enum] > REASON_PRIORITY[entry.reason]:
                    self._bump(self._by_reason, entry.reason.value, -1)
                    self._bump(self._by_category, entry.category.value, -1)
                    self._bump(self._by_reason, reason_enum.value, 1)
                    self._bump(self._by_category, category.value, 1)
                    entry.reason = reason_enum
                    entry.category = category
                    entry.stage = stage
                    entry.source = source
                    if detail:
                        entry.detail = dict(detail)
            return
        self._seen.add(key)
        self._by_reason[reason_enum.value] = self._by_reason.get(reason_enum.value, 0) + 1
        self._by_category[category.value] = self._by_category.get(category.value, 0) + 1
        session_key = str(session)
        self._by_session[session_key] = self._by_session.get(session_key, 0) + 1
        self._by_symbol[str(symbol)] = self._by_symbol.get(str(symbol), 0) + 1
        if len(self._entries) < self._max_entries:
            self._entries[key] = BarDrop(
                symbol=str(symbol),
                session=session,
                timeframe=str(timeframe),
                reason=reason_enum,
                category=category,
                stage=stage,
                source=source,
                slot=str(slot),
                detail=dict(detail or {}),
            )
        else:
            self._dropped_entries += 1

    def extend(self, entries: Iterable[BarDrop]) -> None:
        for entry in entries:
            if entry.key in self._seen:
                existing = self._entries.get(entry.key)
                if existing is None:
                    self._total_events += entry.occurrences
                    continue
                existing.occurrences += entry.occurrences
                if REASON_PRIORITY[entry.reason] > REASON_PRIORITY[existing.reason]:
                    self._bump(self._by_reason, existing.reason.value, -1)
                    self._bump(self._by_category, existing.category.value, -1)
                    self._bump(self._by_reason, entry.reason.value, 1)
                    self._bump(self._by_category, entry.category.value, 1)
                    existing.reason = entry.reason
                    existing.category = entry.category
                    existing.stage = entry.stage
                    existing.source = entry.source
                    existing.detail = dict(entry.detail)
                self._total_events += entry.occurrences
                continue
            self._seen.add(entry.key)
            self._total_events += entry.occurrences
            self._by_reason[entry.reason.value] = self._by_reason.get(entry.reason.value, 0) + 1
            self._by_category[entry.category.value] = self._by_category.get(entry.category.value, 0) + 1
            session_key = str(entry.session)
            self._by_session[session_key] = self._by_session.get(session_key, 0) + 1
            self._by_symbol[entry.symbol] = self._by_symbol.get(entry.symbol, 0) + 1
            if len(self._entries) < self._max_entries:
                self._entries[entry.key] = BarDrop(
                    symbol=entry.symbol,
                    session=entry.session,
                    timeframe=entry.timeframe,
                    reason=entry.reason,
                    category=entry.category,
                    stage=entry.stage,
                    source=entry.source,
                    slot=entry.slot,
                    detail=dict(entry.detail),
                    occurrences=entry.occurrences,
                )
            else:
                self._dropped_entries += 1

    def entries(self) -> list[BarDrop]:
        return list(self._entries.values())

    def summary(
        self,
        *,
        expected_symbol_sessions: int | None = None,
        sample_limit: int = DEFAULT_SAMPLE_LIMIT,
    ) -> dict[str, Any]:
        total_bars = len(self._seen)
        by_reason = dict(sorted(self._by_reason.items(), key=lambda item: (-item[1], item[0])))
        ratios = {
            reason: round(count * 100.0 / total_bars, 2)
            for reason, count in by_reason.items()
        } if total_bars else {}
        top_symbols = [
            {"symbol": symbol, "count": count}
            for symbol, count in sorted(self._by_symbol.items(), key=lambda item: (-item[1], item[0]))[:20]
        ]
        samples = [entry.to_dict() for entry in list(self._entries.values())[:sample_limit]]
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "total_bars": total_bars,
            "total_events": self._total_events,
            "by_reason": by_reason,
            "by_category": dict(sorted(self._by_category.items())),
            "by_reason_ratio_pct": ratios,
            "by_session": dict(sorted(self._by_session.items())),
            "top_symbols": top_symbols,
            "reason_catalog": {
                reason.value: {
                    "category": REASON_CATEGORY[reason].value,
                    "alert": reason in ALERT_REASONS,
                    "description": REASON_DESCRIPTION[reason],
                }
                for reason in BarDropReason
            },
            "alerts": [
                {"reason": reason, "count": count, "category": REASON_CATEGORY[BarDropReason(reason)].value}
                for reason, count in by_reason.items()
                if BarDropReason(reason) in ALERT_REASONS
            ],
            "expected_symbol_sessions": expected_symbol_sessions,
            "drop_ratio_pct": (
                round(total_bars * 100.0 / expected_symbol_sessions, 4)
                if expected_symbol_sessions
                else None
            ),
            "samples": samples,
            "sample_limit": sample_limit,
            "truncated": len(self._entries) > sample_limit or self._dropped_entries > 0,
            "dropped_entries": self._dropped_entries,
        }

    @classmethod
    def from_summary(cls, payload: Mapping[str, Any]) -> "BarDropLedger":
        """Rebuild a ledger from a previously produced summary."""
        ledger = cls()
        for sample in payload.get("samples", []):
            try:
                reason = BarDropReason(str(sample.get("reason")))
            except ValueError:
                continue
            ledger.record(
                symbol=str(sample.get("symbol", "")),
                session=str(sample.get("session", "")),
                timeframe=str(sample.get("timeframe", "")),
                reason=reason,
                stage=str(sample.get("stage", "")),
                source=str(sample.get("source", "")),
                slot=str(sample.get("slot", "")),
                detail=dict(sample.get("detail") or {}),
            )
        return ledger


__all__ = [
    "ALERT_REASONS",
    "BarDrop",
    "BarDropCategory",
    "BarDropLedger",
    "BarDropReason",
    "LEDGER_SCHEMA_VERSION",
    "REASON_CATEGORY",
    "REASON_DESCRIPTION",
    "REASON_PRIORITY",
]
