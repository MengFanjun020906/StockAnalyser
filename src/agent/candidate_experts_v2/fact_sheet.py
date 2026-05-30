# -*- coding: utf-8 -*-
"""Deterministic FactSheet builder (选股链路重构 P2).

The FactSheet is the shared, deterministic fact底表 every thesis desk reads. It is
computed **once per stock** and only states falsifiable facts — it never scores or
ranks. Two phases:

- **Phase A (本地/无网络)**: position / trend / volume / rsi / liquidity computed from
  local daily bars. Always runs.
- **Phase B (在线/可选/非阻塞)**: capital_direction / sector context passed in by the
  caller after a best-effort online fetch. Missing → stays "unknown".

Red-line bools (``capital_violent_outflow`` / ``breakdown_accelerating``) are
**threshold-gated**: when the corresponding threshold is ``None`` (config 留空) the
bool stays ``False`` and never触发 veto — 避免误杀。See ``veto_gate``.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from src.agent.candidate_experts_v2.schemas import FactSheet


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except Exception:
        return None


def _normalize_bars(daily_df: Any) -> List[Dict[str, Any]]:
    """Normalize a DataFrame / list-of-rows into an oldest→newest list of dicts."""
    if daily_df is None:
        return []
    # pandas DataFrame
    to_dict = getattr(daily_df, "to_dict", None)
    if callable(to_dict) and hasattr(daily_df, "columns"):
        try:
            rows = daily_df.to_dict("records")
        except Exception:
            rows = []
    elif isinstance(daily_df, Sequence) and not isinstance(daily_df, (str, bytes)):
        rows = [dict(r) for r in daily_df if isinstance(r, dict)]
    else:
        rows = []
    return [r for r in rows if isinstance(r, dict)]


def _closes(bars: List[Dict[str, Any]]) -> List[float]:
    out: List[float] = []
    for bar in bars:
        value = _safe_float(bar.get("close"))
        if value is not None:
            out.append(value)
    return out


def _ma(values: Sequence[float], window: int) -> Optional[float]:
    if len(values) < window or window <= 0:
        return None
    chunk = values[-window:]
    return sum(chunk) / len(chunk)


def _rsi14(closes: Sequence[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    # Wilder's smoothing seeded on the first ``period`` deltas.
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _range_pct(closes: Sequence[float], highs: Sequence[float], lows: Sequence[float], window: int) -> Optional[float]:
    if len(closes) < 2:
        return None
    hi_window = [h for h in highs[-window:] if h is not None]
    lo_window = [lo for lo in lows[-window:] if lo is not None]
    if not hi_window or not lo_window:
        return None
    high = max(hi_window)
    low = min(lo_window)
    if high <= low:
        return None
    last = closes[-1]
    return max(0.0, min(1.0, (last - low) / (high - low)))


def intraday_bucket(now: Optional[datetime] = None) -> str:
    """Cache bucket: A股交易时段每30分钟一桶(跳过午休), 收盘后统一 ``eod``.

    上午 09:30-11:30 → ``b1``..``b4``; 下午 13:00-15:00 → ``b5``..``b8``;
    午休(11:30-13:00) → ``midday``; 盘前/盘后/非交易时段 → ``eod``.
    """
    now = now or datetime.now()
    minutes = now.hour * 60 + now.minute
    am_open, am_close = 9 * 60 + 30, 11 * 60 + 30
    pm_open, pm_close = 13 * 60, 15 * 60
    if am_open <= minutes < am_close:
        return f"b{(minutes - am_open) // 30 + 1}"
    if pm_open <= minutes < pm_close:
        return f"b{(minutes - pm_open) // 30 + 5}"
    if am_close <= minutes < pm_open:
        return "midday"
    return "eod"


def fact_sheet_cache_key(code: str, *, date: str, now: Optional[datetime] = None) -> str:
    return f"{code}:{date}:{intraday_bucket(now)}"


def build_fact_sheet(
    code: str,
    daily_df: Any = None,
    *,
    market: str = "cn",
    hard_risk_flags: Optional[List[str]] = None,
    min_avg_turnover: Optional[float] = None,
    capital_direction: str = "unknown",
    capital_metrics: Optional[Dict[str, Any]] = None,
    sector_name: str = "",
    sector_strength: str = "unknown",
    sector_rank_pct: Optional[float] = None,
    leader_already_up: Optional[bool] = None,
    violent_outflow_threshold: Optional[float] = None,
    breakdown_accel_threshold: Optional[float] = None,
    freshness: str = "unknown",
) -> FactSheet:
    """Build a deterministic FactSheet for one stock.

    Phase A reads ``daily_df`` (pandas DataFrame or list of OHLCV dicts, oldest→newest)
    to derive position / trend / volume / rsi / liquidity. Phase B fields
    (``capital_direction`` / sector*) are passed in by the caller; defaults keep them
    ``unknown`` so a missing online fetch never breaks the底表.
    """
    warnings: List[str] = []
    sheet = FactSheet(code=str(code or "").strip())
    sheet.market = market  # type: ignore[attr-defined]
    sheet.freshness = freshness
    sheet.hard_risk_flags = list(hard_risk_flags or [])

    # --- Phase B (passed-in, deterministic relative to inputs) --------------
    if capital_direction in ("inflow", "outflow", "neutral", "unknown"):
        sheet.capital_direction = capital_direction  # type: ignore[assignment]
    if sector_strength in ("strong", "neutral", "weak", "unknown"):
        sheet.sector_strength = sector_strength  # type: ignore[assignment]
    sheet.sector_name = str(sector_name or "")
    sheet.sector_rank_pct = _safe_float(sector_rank_pct)
    sheet.leader_already_up = leader_already_up

    bars = _normalize_bars(daily_df)
    closes = _closes(bars)
    highs = [_safe_float(b.get("high")) for b in bars]
    lows = [_safe_float(b.get("low")) for b in bars]
    volumes = [_safe_float(b.get("volume")) for b in bars]
    turnovers = [_safe_float(b.get("turnover")) for b in bars]

    if len(closes) < 2:
        warnings.append("insufficient_daily_bars")
        sheet.warnings = warnings
        sheet = _apply_red_lines(
            sheet,
            capital_metrics=capital_metrics,
            violent_outflow_threshold=violent_outflow_threshold,
            breakdown_accel_threshold=breakdown_accel_threshold,
        )
        return sheet

    last_close = closes[-1]

    # --- Position -----------------------------------------------------------
    sheet.range_pct_60 = _round(_range_pct(closes, highs, lows, 60))
    sheet.range_pct_120 = _round(_range_pct(closes, highs, lows, 120))
    high20 = [h for h in highs[-20:] if h is not None]
    if high20:
        peak20 = max(high20)
        if peak20 > 0:
            sheet.dist_to_high_20 = _round((last_close - peak20) / peak20 * 100.0)
    if len(closes) >= 6:
        base5 = closes[-6]
        if base5 > 0:
            sheet.gain_5d = _round((last_close - base5) / base5 * 100.0)

    # --- Trend / MA bias ----------------------------------------------------
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    prev_ma20 = _ma(closes[:-1], 20)
    if ma20 and ma20 > 0:
        sheet.bias_ma20 = _round((last_close - ma20) / ma20 * 100.0)
    sheet.trend_state = _trend_state(last_close, ma20, ma60, prev_ma20)  # type: ignore[assignment]

    # --- Volume ratio (last vs prior-20 average, excluding last) -------------
    valid_vol = [v for v in volumes if v is not None]
    if len(valid_vol) >= 2:
        last_vol = valid_vol[-1]
        prior = valid_vol[-21:-1] if len(valid_vol) > 1 else []
        prior = [v for v in prior if v is not None]
        vol_ma = sum(prior) / len(prior) if prior else None
        if vol_ma and vol_ma > 0:
            sheet.volume_ratio = _round(last_vol / vol_ma)

    # --- RSI ----------------------------------------------------------------
    sheet.rsi14 = _round(_rsi14(closes))

    # --- Liquidity ----------------------------------------------------------
    valid_turnover = [t for t in turnovers[-20:] if t is not None]
    if valid_turnover:
        avg_turnover = sum(valid_turnover) / len(valid_turnover)
        sheet.avg_turnover_20 = _round(avg_turnover, 2)
        if min_avg_turnover is not None and min_avg_turnover > 0:
            sheet.liquidity_ok = avg_turnover >= min_avg_turnover

    sheet.warnings = warnings
    sheet = _apply_red_lines(
        sheet,
        capital_metrics=capital_metrics,
        violent_outflow_threshold=violent_outflow_threshold,
        breakdown_accel_threshold=breakdown_accel_threshold,
    )
    return sheet


def _trend_state(
    last_close: float,
    ma20: Optional[float],
    ma60: Optional[float],
    prev_ma20: Optional[float],
) -> str:
    if ma20 is None or ma60 is None:
        return "unknown"
    ma20_rising = prev_ma20 is not None and ma20 >= prev_ma20
    if last_close > ma20 > ma60 and ma20_rising:
        return "bullish"
    if last_close < ma20 < ma60 and not ma20_rising:
        return "bearish"
    return "neutral"


def _apply_red_lines(
    sheet: FactSheet,
    *,
    capital_metrics: Optional[Dict[str, Any]],
    violent_outflow_threshold: Optional[float],
    breakdown_accel_threshold: Optional[float],
) -> FactSheet:
    """Set红线 bools, strictly threshold-gated (None → never触发).

    ``capital_violent_outflow``: 仅当资金 outflow + 净流出强度 ≥ 阈值时为 True。
    ``breakdown_accelerating``: 仅当趋势 bearish + 近5日跌幅 ≥ 阈值 + 跌破 MA20 时为 True。
    """
    sheet.capital_violent_outflow = False
    sheet.breakdown_accelerating = False

    if violent_outflow_threshold is not None and sheet.capital_direction == "outflow":
        outflow_strength = None
        if isinstance(capital_metrics, dict):
            for key in ("net_outflow_pct", "net_outflow_ratio", "main_net_outflow_pct"):
                outflow_strength = _safe_float(capital_metrics.get(key))
                if outflow_strength is not None:
                    break
        if outflow_strength is not None and abs(outflow_strength) >= violent_outflow_threshold:
            sheet.capital_violent_outflow = True

    if (
        breakdown_accel_threshold is not None
        and sheet.trend_state == "bearish"
        and sheet.gain_5d is not None
        and sheet.gain_5d <= -abs(breakdown_accel_threshold)
        and sheet.bias_ma20 is not None
        and sheet.bias_ma20 < 0
    ):
        sheet.breakdown_accelerating = True

    return sheet


def _round(value: Optional[float], ndigits: int = 4) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), ndigits)
    except Exception:
        return None
