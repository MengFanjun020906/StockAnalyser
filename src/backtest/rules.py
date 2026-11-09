# -*- coding: utf-8 -*-
"""A-share market-rule resolution with explicit verification flags.

The engine never pretends that inferred board rules are verified historical
metadata.  Missing ST/listing/instrument facts remain visible as quality flags
and can downgrade a run to ``partial``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from src.agent.pricing.schemas import MarketRuleSnapshot


TICK = Decimal("0.01")
LOT = 100


@dataclass(frozen=True)
class MarketRule:
    symbol: str
    session: date
    venue: str
    board: str
    tick_size: Decimal = TICK
    buy_lot_size: int = LOT
    price_limit_ratio: Decimal | None = None
    previous_close: Decimal | None = None
    limit_up: Decimal | None = None
    limit_down: Decimal | None = None
    t_plus_one: bool = True
    suspended: bool = False
    st_status: bool = False
    st_status_verified: bool = False
    listing_status: str = "listed"
    listing_status_verified: bool = False
    instrument_type: str = "common_stock"
    instrument_type_verified: bool = False
    rule_source: str = "derived"
    rule_version: str = "cn-market-rules.v1"
    flags: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return (
            self.st_status_verified
            and self.listing_status_verified
            and self.instrument_type_verified
        )

    @property
    def supported(self) -> bool:
        return self.board in {"main", "chinext", "star"} and not self.flags_unsupported

    @property
    def flags_unsupported(self) -> bool:
        return "unsupported_board" in self.flags or "unsupported_instrument" in self.flags

    def limit_price(self, *, up: bool) -> Decimal | None:
        if up:
            return self.limit_up
        return self.limit_down


@dataclass
class MarketRuleBook:
    """Lookup table with deterministic fallback metadata."""

    rules: dict[tuple[str, date], MarketRule] = field(default_factory=dict)
    default_previous_close: dict[tuple[str, date], Decimal] = field(default_factory=dict)
    version: str = "cn-market-rules.v1"

    def get(self, symbol: str, session: date) -> MarketRule | None:
        return self.rules.get((_normalize_symbol(symbol), session))

    def put(self, rule: MarketRule) -> None:
        self.rules[(rule.symbol, rule.session)] = rule

    def quality_flags(self) -> list[str]:
        flags: set[str] = set()
        for rule in self.rules.values():
            flags.update(rule.flags)
            if not rule.verified:
                flags.add("market_rules_unverified")
            if rule.board == "bse":
                flags.add("unsupported_board")
        return sorted(flags)

    def all_verified(self) -> bool:
        return all(rule.verified for rule in self.rules.values())


def normalize_cn_symbol(symbol: str) -> str:
    digits = "".join(ch for ch in str(symbol or "") if ch.isdigit())[-6:]
    return digits if len(digits) == 6 else ""


def _normalize_symbol(symbol: str) -> str:
    return normalize_cn_symbol(symbol) or str(symbol or "").strip().upper()


def board_for_symbol(symbol: str) -> tuple[str, str]:
    digits = normalize_cn_symbol(symbol)
    if not digits:
        return "unknown", "unknown"
    if digits.startswith(("688", "689")):
        return "sse", "star"
    if digits.startswith(("300", "301")):
        return "szse", "chinext"
    if digits.startswith(("8", "4", "92")):
        return "bse", "bse"
    if digits.startswith(("5", "6", "9")):
        return "sse", "main"
    return "szse", "main"


def limit_ratio_for(symbol: str, session: date, *, st: bool = False) -> Decimal:
    _, board = board_for_symbol(symbol)
    if board == "bse":
        return Decimal("0.30")
    if board in {"chinext", "star"}:
        # ChiNext moved from 10% to 20% on 2020-08-24.  STAR opened at 20% on
        # 2019-07-22; earlier dates should not be traded by this engine.
        if board == "chinext" and session < date(2020, 8, 24):
            base = Decimal("0.10")
        elif board == "star" and session < date(2019, 7, 22):
            base = Decimal("0.10")
        else:
            base = Decimal("0.20")
        return Decimal("0.20") if st else base
    return Decimal("0.05") if st else Decimal("0.10")


def limit_price(previous_close: Decimal | None, ratio: Decimal | None, tick: Decimal = TICK, *, up: bool) -> Decimal | None:
    if previous_close is None or ratio is None or previous_close <= 0:
        return None
    multiplier = Decimal("1") + ratio if up else Decimal("1") - ratio
    return (previous_close * multiplier / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


def derive_rule(
    symbol: str,
    session: date,
    *,
    previous_close: Decimal | None,
    st_status: bool = False,
    st_status_verified: bool = False,
    listing_status: str = "listed",
    listing_status_verified: bool = False,
    instrument_type: str = "common_stock",
    instrument_type_verified: bool = False,
    suspended: bool = False,
    flags: tuple[str, ...] = (),
) -> MarketRule:
    venue, board = board_for_symbol(symbol)
    normalized = normalize_cn_symbol(symbol)
    ratio = limit_ratio_for(normalized, session, st=st_status)
    flag_set = set(flags)
    if board == "unknown":
        flag_set.add("unsupported_board")
    if board == "bse":
        flag_set.add("unsupported_board")
        flag_set.add("bse_rules_not_supported")
    if instrument_type != "common_stock":
        flag_set.add("unsupported_instrument")
    if not st_status_verified:
        flag_set.add("st_status_unverified")
    if not listing_status_verified:
        flag_set.add("listing_status_unverified")
    if not instrument_type_verified:
        flag_set.add("instrument_type_unverified")
    if previous_close is None:
        flag_set.add("missing_previous_close")
    if listing_status in {"ipo_special_period", "pre_listing"}:
        ratio = None
        flag_set.add("ipo_no_price_limit")
    return MarketRule(
        symbol=normalized,
        session=session,
        venue=venue,
        board=board,
        tick_size=TICK,
        buy_lot_size=LOT,
        price_limit_ratio=ratio,
        previous_close=previous_close,
        limit_up=limit_price(previous_close, ratio, up=True),
        limit_down=limit_price(previous_close, ratio, up=False),
        t_plus_one=True,
        suspended=suspended,
        st_status=st_status,
        st_status_verified=st_status_verified,
        listing_status=listing_status,
        listing_status_verified=listing_status_verified,
        instrument_type=instrument_type,
        instrument_type_verified=instrument_type_verified,
        rule_source="symbol_board_inference",
        rule_version="cn-market-rules.v1",
        flags=tuple(sorted(flag_set)),
    )


def rule_from_snapshot(snapshot: MarketRuleSnapshot, session: date) -> MarketRule:
    flags: set[str] = set()
    if not snapshot.st_status_verified:
        flags.add("st_status_unverified")
    if not snapshot.listing_status_verified:
        flags.add("listing_status_unverified")
    if not snapshot.instrument_type_verified:
        flags.add("instrument_type_unverified")
    if snapshot.board == "bse":
        flags.add("unsupported_board")
    if snapshot.listing_status in {"ipo_special_period", "pre_listing"}:
        flags.add("ipo_no_price_limit")
    return MarketRule(
        symbol=normalize_cn_symbol(snapshot.symbol or ""),
        session=session,
        venue=snapshot.venue,
        board=snapshot.board,
        tick_size=snapshot.tick_size,
        buy_lot_size=snapshot.buy_lot_size,
        price_limit_ratio=snapshot.price_limit_ratio,
        previous_close=snapshot.previous_close,
        limit_up=snapshot.limit_up,
        limit_down=snapshot.limit_down,
        t_plus_one=snapshot.t_plus_one,
        suspended=snapshot.suspended,
        st_status=snapshot.st_status,
        st_status_verified=snapshot.st_status_verified,
        listing_status=snapshot.listing_status,
        listing_status_verified=snapshot.listing_status_verified,
        instrument_type=snapshot.instrument_type,
        instrument_type_verified=snapshot.instrument_type_verified,
        rule_source=snapshot.rule_source,
        rule_version=snapshot.rule_version,
        flags=tuple(sorted(flags)),
    )


__all__ = [
    "LOT",
    "MarketRule",
    "MarketRuleBook",
    "TICK",
    "board_for_symbol",
    "derive_rule",
    "limit_price",
    "limit_ratio_for",
    "normalize_cn_symbol",
    "rule_from_snapshot",
]
