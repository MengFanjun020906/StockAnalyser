# -*- coding: utf-8 -*-
"""Full-history chart data and deterministic indicator overlays."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import desc, select

from data_provider.base import canonical_stock_code, normalize_stock_code
from src.agent.scoring import CompositeSnapshotBuilder, score_symbol_composite
from src.agent.structure import analyze_price_structure, coerce_price_bars
from src.core.trading_calendar import get_effective_trading_date, get_market_for_stock
from src.services.history_loader import refresh_history_tail
from src.storage import PortfolioCorporateAction, StockDaily, get_db


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 6) if math.isfinite(number) else None


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value) if value is not None else None


class SignalMarketDataService:
    """Load the local cash-price series, refresh its tail, and build chart lines."""

    def __init__(self, db_manager=None) -> None:
        self.db = db_manager or get_db()

    def load_chart(
        self,
        stock_code: str,
        *,
        market: Optional[str] = None,
        account_id: Optional[int] = None,
        refresh_tail: bool = True,
        as_of: Optional[date] = None,
        holding: bool = False,
    ) -> Dict[str, Any]:
        normalized = canonical_stock_code(normalize_stock_code(stock_code)) or str(stock_code).strip().upper()
        resolved_market = (market or get_market_for_stock(normalized) or "cn").lower()
        target_date = as_of or get_effective_trading_date(resolved_market)
        frame, local_meta = self._load_local_frame(normalized, target_date=target_date)
        refresh_meta = {"attempted": False, "source": None, "saved_count": 0, "error": None}

        local_latest = frame["date"].max().date() if not frame.empty else None
        if refresh_tail and (local_latest is None or local_latest < target_date):
            refresh_meta["attempted"] = True
            refreshed, source, saved = refresh_history_tail(
                normalized,
                days=520,
                target_date=target_date,
                persist=True,
            )
            refresh_meta.update({"source": source, "saved_count": saved})
            if refreshed is None or refreshed.empty:
                refresh_meta["error"] = "network_tail_unavailable"
            frame, local_meta = self._load_local_frame(normalized, target_date=target_date)

        if frame.empty:
            return {
                "stock_code": normalized,
                "market": resolved_market,
                "status": "missing",
                "bars": [],
                "indicators": {},
                "structure": {},
                "composite": None,
                "corporate_actions": [],
                "data_window": {
                    "start": None,
                    "end": None,
                    "bar_count": 0,
                    "target_date": target_date.isoformat(),
                    "sources": [],
                    "refresh": refresh_meta,
                },
            }

        indicators = self._indicator_series(frame)
        bars = []
        for index, row in frame.iterrows():
            bars.append({
                "date": row["date"].date().isoformat(),
                "open": _finite(row.get("open")),
                "high": _finite(row.get("high")),
                "low": _finite(row.get("low")),
                "close": _finite(row.get("close")),
                "volume": _finite(row.get("volume")),
                "amount": _finite(row.get("amount")),
                "pct_change": _finite(row.get("pct_chg")),
                "source": row.get("data_source"),
                "guide_line": indicators["guide_line"][index],
                "ma57": indicators["ma57"][index],
                "volume_ma5": indicators["volume_ma5"][index],
                "dif": indicators["dif"][index],
                "dea": indicators["dea"][index],
                "macd_histogram": indicators["macd_histogram"][index],
            })

        scoring_builder = CompositeSnapshotBuilder(
            history_loader=lambda *_args, **_kwargs: (frame.copy(), "signal_preview_local_db"),
            fallback_to_network=False,
        )
        composite = score_symbol_composite(
            normalized,
            as_of=frame["date"].iloc[-1].date(),
            quote_basis="latest_trading_day",
            holding=holding,
            snapshot_builder=scoring_builder,
        )
        structure_frame = frame.tail(520).reset_index(drop=True)
        structure = analyze_price_structure(
            coerce_price_bars(structure_frame.to_dict("records")),
            min_bars=30,
        )

        return {
            "stock_code": normalized,
            "market": resolved_market,
            "status": "ok",
            "bars": bars,
            "structure": self._chart_structure(structure, structure_frame=structure_frame),
            "composite": composite,
            "corporate_actions": self._corporate_actions(
                normalized,
                account_id=account_id,
                start_date=frame["date"].iloc[0].date(),
                end_date=frame["date"].iloc[-1].date(),
            ),
            "data_window": {
                "start": frame["date"].iloc[0].date().isoformat(),
                "end": frame["date"].iloc[-1].date().isoformat(),
                "bar_count": len(frame),
                "target_date": target_date.isoformat(),
                "sources": local_meta["sources"],
                "storage_codes": local_meta["storage_codes"],
                "refresh": refresh_meta,
                "price_basis": "local_cash_price_unadjusted",
            },
        }

    def _load_local_frame(self, stock_code: str, *, target_date: date) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        variants = self._storage_code_variants(stock_code)
        with self.db.get_session() as session:
            rows = session.execute(
                select(StockDaily)
                .where(StockDaily.code.in_(variants), StockDaily.date <= target_date)
                .order_by(StockDaily.date, desc(StockDaily.updated_at), desc(StockDaily.id))
            ).scalars().all()
        if not rows:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"]), {
                "sources": [],
                "storage_codes": [],
            }

        preferred = canonical_stock_code(normalize_stock_code(stock_code)) or stock_code
        records: Dict[date, Dict[str, Any]] = {}
        priorities: Dict[date, Tuple[int, datetime, int]] = {}
        for row in rows:
            priority = (
                1 if row.code == preferred else 0,
                row.updated_at or row.created_at or datetime.min,
                int(row.id or 0),
            )
            if row.date in priorities and priorities[row.date] >= priority:
                continue
            priorities[row.date] = priority
            records[row.date] = row.to_dict()
            records[row.date]["data_source"] = row.data_source
            records[row.date]["storage_code"] = row.code
        frame = pd.DataFrame(records.values()).sort_values("date").reset_index(drop=True)
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame.dropna(subset=["date", "open", "high", "low", "close"])
        for column in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
            if column not in frame.columns:
                frame[column] = None
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame.reset_index(drop=True), {
            "sources": sorted({str(value) for value in frame["data_source"].dropna().tolist()}),
            "storage_codes": sorted({str(value) for value in frame["storage_code"].dropna().tolist()}),
        }

    @staticmethod
    def _storage_code_variants(stock_code: str) -> List[str]:
        raw = str(stock_code or "").strip().upper()
        normalized = normalize_stock_code(raw)
        canonical = canonical_stock_code(normalized) or normalized or raw
        values = [value for value in (raw, normalized, canonical) if value]
        if canonical.isdigit() and len(canonical) == 6:
            suffix = "SH" if canonical.startswith(("5", "6")) else "BJ" if canonical.startswith(("8", "9")) else "SZ"
            values.extend([f"{canonical}.{suffix}", f"{suffix}{canonical}"])
        return list(dict.fromkeys(values))

    @staticmethod
    def _indicator_series(frame: pd.DataFrame) -> Dict[str, List[Optional[float]]]:
        close = pd.to_numeric(frame["close"], errors="coerce")
        volume = pd.to_numeric(frame["volume"], errors="coerce")
        guide = close.ewm(span=13, adjust=False).mean()
        ma57 = close.rolling(57, min_periods=57).mean()
        dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        dea = dif.ewm(span=9, adjust=False).mean()
        histogram = (dif - dea) * 2.0
        volume_ma5 = volume.rolling(5, min_periods=5).mean()

        def _values(series: pd.Series, *, warmup: int = 0) -> List[Optional[float]]:
            result = []
            for index, value in enumerate(series.tolist()):
                result.append(None if index + 1 < warmup else _finite(value))
            return result

        return {
            "guide_line": _values(guide, warmup=200),
            "ma57": _values(ma57, warmup=57),
            "volume_ma5": _values(volume_ma5, warmup=5),
            "dif": _values(dif, warmup=200),
            "dea": _values(dea, warmup=200),
            "macd_histogram": _values(histogram, warmup=200),
        }

    @staticmethod
    def _chart_structure(
        structure: Dict[str, Any],
        *,
        structure_frame: pd.DataFrame,
    ) -> Dict[str, Any]:
        if not isinstance(structure, dict):
            return {}
        chan = structure.get("chan") if isinstance(structure.get("chan"), dict) else {}
        dates = [value.date().isoformat() for value in structure_frame["date"].tolist()]
        centers = []
        for raw in chan.get("latest_centers") or []:
            center = dict(raw)
            start_index = int(center.get("start_index") or 0)
            end_index = int(center.get("end_index") or 0)
            if 0 <= start_index < len(dates):
                center["start_date"] = dates[start_index]
            if 0 <= end_index < len(dates):
                center["end_date"] = dates[end_index]
            centers.append(center)
        return {
            "status": structure.get("status"),
            "data_quality": structure.get("data_quality"),
            "pens": chan.get("latest_pens") or [],
            "centers": centers,
            "unfinished_pen": chan.get("unfinished_pen") or {},
            "signals": chan.get("signals") or [],
        }

    def _corporate_actions(
        self,
        stock_code: str,
        *,
        account_id: Optional[int],
        start_date: date,
        end_date: date,
    ) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            stmt = select(PortfolioCorporateAction).where(
                PortfolioCorporateAction.symbol == stock_code,
                PortfolioCorporateAction.effective_date >= start_date,
                PortfolioCorporateAction.effective_date <= end_date,
            )
            if account_id is not None:
                stmt = stmt.where(PortfolioCorporateAction.account_id == account_id)
            rows = session.execute(stmt.order_by(PortfolioCorporateAction.effective_date)).scalars().all()
            return [
                {
                    "date": _iso(row.effective_date),
                    "action_type": row.action_type,
                    "cash_dividend_per_share": _finite(row.cash_dividend_per_share),
                    "split_ratio": _finite(row.split_ratio),
                    "note": row.note,
                }
                for row in rows
            ]


__all__ = ["SignalMarketDataService"]
