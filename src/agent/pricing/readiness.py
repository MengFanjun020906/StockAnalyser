from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import inspect

from src.storage import DatabaseManager

from .fingerprint import fingerprint
from .market_rules import current_cn_common_stock_metadata, normalize_cn_symbol
from .params import PARAMS_REGISTRY, PRICE_PLAN_PARAMS_V1
from .repository import PricePlanRepository


REQUIRED_TABLES = frozenset(
    {
        "trade_plan_artifacts",
        "trade_plan_trace_links",
        "plan_review_artifacts",
        "account_plan_decisions",
        "plan_lifecycle_events",
        "plan_lifecycle_states",
        "market_rule_snapshots",
        "chip_distribution_snapshots",
    }
)


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    checks: dict[str, bool]
    reason_codes: tuple[str, ...]


class PricePlanReadinessGate:
    def __init__(self, db_manager: DatabaseManager | None = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    def evaluate(self, *, symbol: str, as_of: date) -> ReadinessResult:
        checks: dict[str, bool] = {}
        checks["params_registered"] = (
            PARAMS_REGISTRY.get(PRICE_PLAN_PARAMS_V1.version) is PRICE_PLAN_PARAMS_V1
        )
        try:
            tables = set(inspect(self.db._engine).get_table_names())
            checks["schema_v1"] = REQUIRED_TABLES.issubset(tables)
        except Exception:
            checks["schema_v1"] = False
        checks["market_rules"] = self._market_rules_ready(symbol=symbol, as_of=as_of)
        checks["fingerprint"] = (
            fingerprint({"price_plan": "self-test", "version": 1})
            == fingerprint({"version": 1, "price_plan": "self-test"})
        )
        ready = all(checks.values())
        return ReadinessResult(
            ready=ready,
            checks=checks,
            reason_codes=() if ready else ("SYSTEM_NOT_READY",),
        )

    def _market_rules_ready(self, *, symbol: str, as_of: date) -> bool:
        try:
            persisted = PricePlanRepository(self.db).get_market_rules_for_symbol_date(
                symbol,
                as_of,
            )
            if persisted is not None:
                return _verified_common_stock_rules(persisted, symbol)
            metadata = current_cn_common_stock_metadata(symbol, trade_date=as_of)
            return bool(
                metadata
                and metadata.get("instrument_type") == "common_stock"
                and metadata.get("instrument_type_verified") is True
                and metadata.get("st_status_verified") is True
                and metadata.get("listing_status") == "listed"
                and metadata.get("listing_status_verified") is True
            )
        except Exception:
            return False


def _verified_common_stock_rules(rules: object, symbol: str) -> bool:
    return bool(
        getattr(rules, "symbol", None) == normalize_cn_symbol(symbol)
        and getattr(rules, "tick_size", 0) > 0
        and getattr(rules, "instrument_type", None) == "common_stock"
        and getattr(rules, "instrument_type_verified", False)
        and getattr(rules, "st_status_verified", False)
        and getattr(rules, "listing_status", None) == "listed"
        and getattr(rules, "listing_status_verified", False)
    )
