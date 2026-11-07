"""Shared runtime entry for selection and single-stock scoring flows."""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.agent.scoring.composite_snapshot import CompositeSnapshotBuilder
from src.agent.scoring.composite_verdict import evaluate_composite_verdict
from src.agent.scoring.strategy4_backtest import evaluate_strategy4_backtest


SCHEMA_VERSION = "stock-composite-shadow.v1"


def score_symbol_composite(
    symbol: str,
    *,
    as_of: Any,
    quote_basis: str = "unknown",
    holding: bool = False,
    snapshot_builder: Optional[CompositeSnapshotBuilder] = None,
    market_regime: Optional[Any] = None,
    backtest_frame: Optional[Any] = None,
    backtest_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build one snapshot and return its shadow-mode mathematical verdict."""
    builder = snapshot_builder or CompositeSnapshotBuilder()
    snapshot = builder.build(
        symbol,
        as_of=as_of,
        quote_basis=quote_basis,
        holding=holding,
    )
    if backtest_frame is not None:
        metadata = backtest_metadata or {}
        snapshot.backtest = evaluate_strategy4_backtest(
            backtest_frame,
            as_of=as_of,
            listing_date=metadata.get("listing_date"),
            is_st=metadata.get("is_st"),
            limit_pct=metadata.get("limit_pct"),
        )
    verdict = evaluate_composite_verdict(snapshot, market_regime=market_regime)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok" if snapshot.data_quality != "insufficient" else "insufficient_data",
        "snapshot": snapshot.model_dump(mode="json"),
        "composite_verdict": verdict.model_dump(mode="json"),
    }


__all__ = ["SCHEMA_VERSION", "score_symbol_composite"]
