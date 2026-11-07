# -*- coding: utf-8 -*-
"""Read-only lifecycle outcome and shadow-calibration evaluation."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select

from src.core.trading_calendar import is_market_open
from src.repositories.signal_lifecycle_repo import SignalLifecycleRepository
from src.services.signal_market_data_service import SignalMarketDataService
from src.storage import StockDaily


MIN_SAMPLES_FOR_REVIEW = 30
MIN_COVERAGE_DAYS_FOR_REVIEW = 60
HORIZONS = (5, 10, 20)
BENCHMARK_SYMBOLS = {"cn": "000300", "hk": "HSI", "us": "SPX"}


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    return None


def _pct(start: Optional[float], end: Optional[float]) -> Optional[float]:
    if start is None or end is None or float(start) == 0:
        return None
    return round((float(end) / float(start) - 1.0) * 100.0, 4)


class SignalEvaluationService:
    """Evaluate persisted signals without mutating lifecycle or event state."""

    _cache: Dict[Tuple[Any, ...], Tuple[float, Dict[str, Any]]] = {}
    _cache_ttl_seconds = 30.0

    def __init__(
        self,
        *,
        repository: Optional[SignalLifecycleRepository] = None,
        market_data: Optional[SignalMarketDataService] = None,
    ) -> None:
        self.repo = repository or SignalLifecycleRepository()
        self.market_data = market_data or SignalMarketDataService(self.repo.db)

    def evaluate_all(self, *, limit: int = 300, setup_type: Optional[str] = None) -> Dict[str, Any]:
        entries = self.repo.list_entry_signals(limit=limit)
        if setup_type:
            entries = [item for item in entries if item.get("setup_type") == setup_type]
        rows = []
        now = time.monotonic()
        for item in entries:
            key = (id(self.repo.db), item.get("signal_id"), item.get("version"), date.today())
            cached = self._cache.get(key)
            if cached and now - cached[0] <= self._cache_ttl_seconds:
                rows.append(deepcopy(cached[1]))
                continue
            evaluated = self.evaluate_signal(item)
            self._cache[key] = (now, deepcopy(evaluated))
            rows.append(evaluated)
        return self._summary(rows)

    def evaluate_signal(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        chart = self.market_data.load_chart(
            signal["stock_code"],
            market=signal.get("market"),
            refresh_tail=False,
            as_of=date.today(),
            holding=False,
        )
        events = self.repo.list_signal_events(signal["signal_id"])
        bars = self._bars_by_date(chart.get("bars") or [])
        trigger = next((event for event in events if event.get("event_type") == "entry_price_triggered"), None)
        trigger_date = _as_date(trigger.get("occurred_at")) if trigger else None
        created_date = _as_date(signal.get("created_as_of"))
        valid_until = _as_date(signal.get("valid_until"))
        triggered = trigger is not None
        anchor_date = trigger_date if triggered else None
        simulated_entry = self._simulated_entry_price(signal, trigger) if triggered else None
        horizon_rows = {
            str(horizon): self._horizon_result(
                bars,
                anchor_date=anchor_date,
                entry_price=simulated_entry,
                horizon=horizon,
                market=signal.get("market") or "cn",
            )
            for horizon in HORIZONS
        }
        if not triggered:
            # An untriggered plan is not a zero-price trade. Keep its directional
            # return out of signal-return statistics while preserving the
            # execution-adjusted convention (no fill -> zero).
            horizon_rows = {
                key: {
                    "status": "not_triggered",
                    "signal_return": None,
                    "signal_return_pct": None,
                    "execution_adjusted_return": 0.0,
                    "execution_adjusted_return_pct": 0.0,
                }
                for key in horizon_rows
            }
        benchmark = self._benchmark_result(
            signal.get("market") or "cn",
            anchor_date=anchor_date,
            horizon_rows=horizon_rows,
        ) if triggered else {"symbol": BENCHMARK_SYMBOLS.get(signal.get("market") or "cn"), "status": "not_triggered", "horizons": {}}
        expired_untriggered = trigger is None and signal.get("status") == "expired"
        opportunity = self._opportunity_cost(bars, created_date, valid_until) if expired_untriggered else {
            "status": "not_applicable",
            "opportunity_cost": None,
            "avoided_loss": None,
        }
        terminal = signal.get("status") in {"expired", "invalidated", "superseded"}
        metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
        execution_eligible = metadata.get("execution_eligible") is not False
        complete_horizons = [
            horizon for horizon, item in horizon_rows.items() if item.get("status") == "complete"
        ]
        sample_eligible = bool(
            execution_eligible
            and chart.get("status") == "ok"
            and signal.get("status") not in {"ambiguous", "needs_review"}
            and (terminal or (triggered and complete_horizons))
        )
        return {
            "signal_id": signal["signal_id"],
            "stock_code": signal["stock_code"],
            "market": signal.get("market"),
            "setup_type": signal.get("setup_type") or "unclassified",
            "status": signal.get("status"),
            "revision": signal.get("revision"),
            "created_as_of": signal.get("created_as_of"),
            "triggered": triggered,
            "triggered_at": trigger.get("occurred_at") if trigger else None,
            "simulated_entry_price": simulated_entry,
            "terminal": terminal,
            "execution_eligible": execution_eligible,
            "plan_mode": metadata.get("plan_mode") or "legacy_entry",
            "sample_eligible": sample_eligible,
            "complete_horizons": complete_horizons,
            "horizons": horizon_rows,
            "benchmark": benchmark,
            "opportunity": opportunity,
            "data_quality": "ok" if chart.get("status") == "ok" else "missing",
            "data_window": chart.get("data_window") or {},
        }

    def _summary(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        eligible = [row for row in rows if row.get("sample_eligible")]
        triggered = [row for row in eligible if row.get("triggered")]
        untriggered = [row for row in eligible if not row.get("triggered")]
        mature = [row for row in eligible if row.get("complete_horizons")]
        summary = {
            "sample_count": len(rows),
            "eligible_count": len(eligible),
            "triggered_count": len(triggered),
            "untriggered_count": len(untriggered),
            "ambiguous_count": sum(row.get("status") == "ambiguous" for row in rows),
            "hit_rate_pct": round(len(triggered) / len(eligible) * 100.0, 2) if eligible else None,
            "average_wait_trading_days": self._average_wait(eligible),
            "execution_adjusted_return_pct": self._average_execution_adjusted(eligible),
            "opportunity_cost_pct": self._average_opportunity(untriggered, "opportunity_cost"),
            "avoided_loss_pct": self._average_opportunity(untriggered, "avoided_loss"),
            "horizons": {
                str(horizon): {
                    "signal_return_pct": self._average_horizon(mature, "signal_return", horizon),
                    "execution_adjusted_return_pct": self._average_execution_adjusted(eligible, horizon),
                    "benchmark_return_pct": self._average_horizon(mature, "benchmark_return", horizon),
                    "excess_return_pct": self._average_horizon(mature, "excess_return", horizon),
                    "sample_count": sum(str(horizon) in row.get("complete_horizons", []) for row in mature),
                    "execution_sample_count": self._execution_sample_count(eligible, horizon),
                }
                for horizon in HORIZONS
            },
        }
        summary["by_setup_type"] = self._by_setup(rows)
        summary["review_policy"] = {
            "min_samples": MIN_SAMPLES_FOR_REVIEW,
            "min_coverage_days": MIN_COVERAGE_DAYS_FOR_REVIEW,
            "mode": "shadow_only",
        }
        summary["rows"] = rows
        return summary

    def _by_setup(self, rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[str(row.get("setup_type") or "unclassified")].append(row)
        result = []
        for setup, items in sorted(groups.items()):
            eligible = [item for item in items if item.get("sample_eligible")]
            coverage = self._coverage_days(eligible)
            ready = len(eligible) >= MIN_SAMPLES_FOR_REVIEW and coverage >= MIN_COVERAGE_DAYS_FOR_REVIEW
            result.append({
                "setup_type": setup,
                "sample_count": len(items),
                "eligible_count": len(eligible),
                "triggered_count": sum(item.get("triggered") for item in eligible),
                "hit_rate_pct": round(sum(item.get("triggered") for item in eligible) / len(eligible) * 100.0, 2) if eligible else None,
                "coverage_days": coverage,
                "review_status": "reviewable" if ready else "insufficient_sample",
                "calibration_applied": False,
            })
        return result

    @staticmethod
    def _bars_by_date(raw_bars: Iterable[Dict[str, Any]]) -> Dict[date, Dict[str, Any]]:
        output = {}
        for bar in raw_bars:
            day = _as_date(bar.get("date"))
            if day:
                output[day] = bar
        return output

    @staticmethod
    def _simulated_entry_price(signal: Dict[str, Any], trigger: Optional[Dict[str, Any]]) -> Optional[float]:
        # The upper edge is the conservative long-entry assumption. Real fills
        # are intentionally out of scope because EntrySignal is account-neutral.
        entry_high = signal.get("entry_high")
        if entry_high is not None:
            return float(entry_high)
        payload = trigger.get("payload") if isinstance(trigger, dict) else {}
        if isinstance(payload, dict):
            for key in ("trigger_price", "price"):
                if payload.get(key) is not None:
                    return float(payload[key])
            minute = payload.get("minute_bar")
            if isinstance(minute, dict) and minute.get("close") is not None:
                return float(minute["close"])
        return None

    @staticmethod
    def _trading_forward_dates(days: Dict[date, Dict[str, Any]], anchor: Optional[date], horizon: int) -> Optional[date]:
        if anchor is None:
            return None
        candidates = [day for day in sorted(days) if day > anchor and is_market_open("cn", day)]
        return candidates[horizon - 1] if len(candidates) >= horizon else None

    def _horizon_result(
        self,
        bars: Dict[date, Dict[str, Any]],
        *,
        anchor_date: Optional[date],
        entry_price: Optional[float],
        horizon: int,
        market: str,
    ) -> Dict[str, Any]:
        if anchor_date is None or entry_price is None:
            return {"status": "missing_anchor", "signal_return": None, "execution_adjusted_return": None}
        candidates = [day for day in sorted(bars) if day > anchor_date and is_market_open(market, day)]
        if len(candidates) < horizon:
            return {"status": "pending", "signal_return": None, "execution_adjusted_return": None}
        end_date = candidates[horizon - 1]
        end_close = bars[end_date].get("close")
        signal_return = _pct(entry_price, float(end_close)) if end_close is not None else None
        return {
            "status": "complete" if signal_return is not None else "missing_data",
            "anchor_date": anchor_date.isoformat(),
            "end_date": end_date.isoformat(),
            "signal_return": signal_return,
            "signal_return_pct": signal_return,
            "execution_adjusted_return": signal_return,
            "execution_adjusted_return_pct": signal_return,
        }

    def _benchmark_result(self, market: str, *, anchor_date: Optional[date], horizon_rows: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        symbol = BENCHMARK_SYMBOLS.get(market)
        if not symbol or anchor_date is None:
            return {"symbol": symbol, "status": "missing"}
        closes = self._benchmark_closes(symbol)
        output = {"symbol": symbol, "status": "ok", "horizons": {}}
        for horizon, row in horizon_rows.items():
            end_date = _as_date(row.get("end_date"))
            start = closes.get(anchor_date) or next((closes[day] for day in sorted(closes) if day >= anchor_date), None)
            end = closes.get(end_date) if end_date else None
            benchmark_return = _pct(start, end)
            row["benchmark_return"] = benchmark_return
            row["benchmark_return_pct"] = benchmark_return
            signal_return = row.get("signal_return")
            if benchmark_return is not None and signal_return is not None:
                row["excess_return"] = round(
                    ((1.0 + float(signal_return) / 100.0) / (1.0 + float(benchmark_return) / 100.0) - 1.0) * 100.0,
                    4,
                )
            else:
                row["excess_return"] = None
            row["excess_return_pct"] = row["excess_return"]
            output["horizons"][horizon] = {"return_pct": benchmark_return, "status": "complete" if benchmark_return is not None else "missing"}
        if not any(item.get("return_pct") is not None for item in output["horizons"].values()):
            output["status"] = "missing"
        return output

    def _benchmark_closes(self, symbol: str) -> Dict[date, float]:
        with self.repo.db.get_session() as session:
            rows = session.execute(
                select(StockDaily).where(StockDaily.code == symbol).order_by(StockDaily.date)
            ).scalars().all()
        return {row.date: float(row.close) for row in rows if row.close is not None}

    @staticmethod
    def _opportunity_cost(bars: Dict[date, Dict[str, Any]], start: Optional[date], end: Optional[date]) -> Dict[str, Any]:
        if start is None or end is None or start not in bars or end not in bars:
            return {"status": "missing_data", "opportunity_cost": None, "avoided_loss": None}
        change = _pct(float(bars[start].get("close")), float(bars[end].get("close")))
        if change is None:
            return {"status": "missing_data", "opportunity_cost": None, "avoided_loss": None}
        return {
            "status": "complete",
            "opportunity_cost": max(0.0, change),
            "avoided_loss": max(0.0, -change),
            "signed_return_pct": change,
        }

    @staticmethod
    def _average_wait(rows: Iterable[Dict[str, Any]]) -> Optional[float]:
        waits = []
        for row in rows:
            created = _as_date(row.get("created_as_of"))
            triggered = _as_date(row.get("triggered_at"))
            if created and triggered:
                waits.append(sum(is_market_open(row.get("market") or "cn", day) for day in _date_range(created, triggered)))
        return round(sum(waits) / len(waits), 2) if waits else None

    @staticmethod
    def _average_horizon(rows: Iterable[Dict[str, Any]], key: str, horizon: Optional[int] = None) -> Optional[float]:
        values = []
        for row in rows:
            if horizon is None:
                for item in (row.get("horizons") or {}).values():
                    if item.get(key) is not None:
                        values.append(float(item[key]))
            else:
                item = (row.get("horizons") or {}).get(str(horizon)) or {}
                if item.get(key) is not None:
                    values.append(float(item[key]))
        return round(sum(values) / len(values), 4) if values else None

    @staticmethod
    def _average_opportunity(rows: Iterable[Dict[str, Any]], key: str) -> Optional[float]:
        values = [float((row.get("opportunity") or {}).get(key)) for row in rows if (row.get("opportunity") or {}).get(key) is not None]
        return round(sum(values) / len(values), 4) if values else None

    @staticmethod
    def _average_execution_adjusted(rows: Iterable[Dict[str, Any]], horizon: Optional[int] = None) -> Optional[float]:
        values = []
        selected = HORIZONS if horizon is None else (horizon,)
        for row in rows:
            if not row.get("sample_eligible"):
                continue
            for item_horizon in selected:
                item = (row.get("horizons") or {}).get(str(item_horizon)) or {}
                if not row.get("triggered"):
                    values.append(0.0)
                elif item.get("status") == "complete" and item.get("execution_adjusted_return") is not None:
                    values.append(float(item["execution_adjusted_return"]))
        return round(sum(values) / len(values), 4) if values else None

    @staticmethod
    def _execution_sample_count(rows: Iterable[Dict[str, Any]], horizon: int) -> int:
        count = 0
        for row in rows:
            if not row.get("sample_eligible"):
                continue
            item = (row.get("horizons") or {}).get(str(horizon)) or {}
            if not row.get("triggered") or item.get("status") == "complete":
                count += 1
        return count

    @staticmethod
    def _coverage_days(rows: Iterable[Dict[str, Any]]) -> int:
        dates = [_as_date(row.get("created_as_of")) for row in rows]
        dates = [item for item in dates if item]
        return (max(dates) - min(dates)).days if len(dates) >= 2 else 0


def _date_range(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current = current.fromordinal(current.toordinal() + 1)


__all__ = ["SignalEvaluationService"]
