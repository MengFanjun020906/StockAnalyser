# -*- coding: utf-8 -*-
"""Build ``AgentUserContext`` from portfolio snapshots."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from src.schemas.agent_context import (
    AccountContext,
    AgentUserContext,
    PositionContext,
    ReportContext,
)
from src.data.stock_index_loader import get_index_stock_name
from src.data.stock_mapping import STOCK_NAME_MAP, is_meaningful_stock_name


def build_agent_user_context_from_portfolio_snapshot(
    snapshot: Dict[str, Any],
    *,
    primary_symbol: Optional[str] = None,
    target_symbols: Optional[List[str]] = None,
    user_prompt: Optional[str] = None,
    analysis_mode: str = "planning_execute",
    report_language: str = "zh",
) -> AgentUserContext:
    """Convert ``PortfolioService.get_portfolio_snapshot`` output to Agent context."""
    accounts: List[AccountContext] = []
    positions: List[PositionContext] = []

    for account in snapshot.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        account_id = _optional_int(account.get("account_id"))
        total_equity = _optional_float(account.get("total_equity"))
        accounts.append(
            AccountContext(
                account_id=account_id,
                account_name=account.get("account_name"),
                broker=account.get("broker"),
                account_type="cash",
                margin_mode="none",
                market=_normalize_market(account.get("market")),
                base_currency=str(account.get("base_currency") or snapshot.get("currency") or "CNY"),
                total_equity=total_equity,
                available_cash=_optional_float(account.get("total_cash")),
                total_market_value=_optional_float(account.get("total_market_value")),
                cost_method=_normalize_cost_method(account.get("cost_method") or snapshot.get("cost_method")),
            )
        )

        for raw_position in account.get("positions") or []:
            if not isinstance(raw_position, dict):
                continue
            symbol = str(raw_position.get("symbol") or "").strip()
            if not symbol:
                continue
            quantity = _optional_float(raw_position.get("quantity")) or 0.0
            if quantity <= 0:
                continue
            market_value = _optional_float(raw_position.get("market_value_base"))
            positions.append(
                PositionContext(
                    symbol=symbol,
                    market=_normalize_market(raw_position.get("market")),
                    account_id=account_id,
                    quantity=quantity,
                    avg_cost=_optional_float(raw_position.get("avg_cost")),
                    total_cost=_optional_float(raw_position.get("total_cost")),
                    last_price=_optional_float(raw_position.get("last_price")),
                    market_value=market_value,
                    unrealized_pnl=_optional_float(raw_position.get("unrealized_pnl_base")),
                    unrealized_pnl_pct=_optional_float(raw_position.get("unrealized_pnl_pct")),
                    position_pct=_position_pct(market_value, total_equity),
                    stock_name=_resolve_stock_name(symbol, raw_position.get("stock_name") or raw_position.get("name")),
                    notes=_position_notes(raw_position),
                )
            )

    resolved_targets = [symbol for symbol in (target_symbols or []) if symbol]
    if primary_symbol and primary_symbol not in resolved_targets:
        resolved_targets.insert(0, primary_symbol)

    context = AgentUserContext(
        accounts=accounts,
        positions=positions,
        report=ReportContext(
            intent="auto",
            analysis_mode=analysis_mode,  # type: ignore[arg-type]
            target_symbols=resolved_targets,
            primary_symbol=primary_symbol,
            language="en" if report_language == "en" else "zh",
            user_prompt=user_prompt,
        ),
        metadata={
            "source": "PortfolioService.get_portfolio_snapshot",
            "snapshot_as_of": snapshot.get("as_of"),
            "snapshot_currency": snapshot.get("currency"),
            "snapshot_cost_method": snapshot.get("cost_method"),
            "snapshot_fx_stale": snapshot.get("fx_stale"),
        },
    )
    return context


def build_agent_user_context_from_portfolio_service(
    portfolio_service: Any,
    *,
    account_id: Optional[int] = None,
    symbol: Optional[str] = None,
    cost_method: str = "fifo",
    as_of: Optional[date] = None,
    user_prompt: Optional[str] = None,
    analysis_mode: str = "planning_execute",
    report_language: str = "zh",
) -> AgentUserContext:
    """Fetch a portfolio snapshot and convert it to ``AgentUserContext``."""
    snapshot = portfolio_service.get_portfolio_snapshot(
        account_id=account_id,
        as_of=as_of,
        cost_method=cost_method,
    )
    return build_agent_user_context_from_portfolio_snapshot(
        snapshot,
        primary_symbol=symbol,
        target_symbols=[symbol] if symbol else None,
        user_prompt=user_prompt,
        analysis_mode=analysis_mode,
        report_language=report_language,
    )


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_market(value: Any) -> str:
    market = str(value or "cn").strip().lower()
    if market in {"cn", "hk", "us"}:
        return market
    return "mixed"


def _normalize_cost_method(value: Any) -> str:
    method = str(value or "fifo").strip().lower()
    return method if method in {"fifo", "avg"} else "fifo"


def _position_pct(market_value: Optional[float], total_equity: Optional[float]) -> Optional[float]:
    if market_value is None or not total_equity:
        return None
    if total_equity <= 0:
        return None
    return round(market_value / total_equity * 100.0, 6)


def _position_notes(raw_position: Dict[str, Any]) -> Optional[str]:
    parts = []
    if raw_position.get("price_available") is False:
        parts.append("price_available=false")
    if raw_position.get("price_stale"):
        parts.append("price_stale=true")
    source = raw_position.get("price_source")
    if source:
        parts.append(f"price_source={source}")
    return "; ".join(parts) if parts else None


def _resolve_stock_name(symbol: str, current_name: Any = None) -> Optional[str]:
    code = str(symbol or "").strip()
    current = str(current_name or "").strip()
    if is_meaningful_stock_name(current, code):
        return current
    for name in (STOCK_NAME_MAP.get(code), get_index_stock_name(code)):
        if is_meaningful_stock_name(name, code):
            return str(name)
    return current or None


__all__ = [
    "build_agent_user_context_from_portfolio_service",
    "build_agent_user_context_from_portfolio_snapshot",
]
