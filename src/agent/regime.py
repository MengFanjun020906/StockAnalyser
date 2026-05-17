# -*- coding: utf-8 -*-
"""A-share market regime detection engine.

The engine is deterministic and data-source agnostic: callers provide OHLCV
bars plus optional A-share sentiment/liquidity components, and the engine
returns a compact market-state snapshot for Agent decision constraints.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Sequence


class VolatilityBucket(str, Enum):
    VERY_LOW = "very_low"
    LOW = "low"
    NORMAL = "normal"
    ELEVATED = "elevated"
    HIGH_VOL = "high_vol"
    EXTREME = "extreme"
    UNKNOWN = "unknown"


class MarketRegime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGE_BOUND = "range_bound"
    HIGH_VOLATILITY = "high_volatility"
    RISK_OFF = "risk_off"
    EVENT_DRIVEN = "event_driven"
    PANIC = "panic"
    UNKNOWN = "unknown"


class SentimentState(str, Enum):
    EXTREME_GREED = "extreme_greed"
    GREED = "greed"
    NEUTRAL = "neutral"
    FEAR = "fear"
    EXTREME_FEAR = "extreme_fear"
    UNKNOWN = "unknown"


class WyckoffPhase(str, Enum):
    ACCUMULATION = "accumulation"
    DISTRIBUTION = "distribution"
    MARKUP = "markup"
    MARKDOWN = "markdown"
    RANGE = "range"
    UNKNOWN = "unknown"


_BUCKET_ORDER = [
    VolatilityBucket.VERY_LOW,
    VolatilityBucket.LOW,
    VolatilityBucket.NORMAL,
    VolatilityBucket.ELEVATED,
    VolatilityBucket.HIGH_VOL,
    VolatilityBucket.EXTREME,
]

_VOL_BUCKET_META: Dict[VolatilityBucket, Dict[str, Any]] = {
    VolatilityBucket.VERY_LOW: {
        "risk_multiplier": 0.75,
        "strategy_hints": ["低波环境，突破信号需要放量确认，避免过度交易。"],
    },
    VolatilityBucket.LOW: {
        "risk_multiplier": 0.85,
        "strategy_hints": ["低波环境，适合等待区间突破或回踩确认。"],
    },
    VolatilityBucket.NORMAL: {
        "risk_multiplier": 1.0,
        "strategy_hints": ["常态波动，按个股证据和账户约束执行。"],
    },
    VolatilityBucket.ELEVATED: {
        "risk_multiplier": 1.2,
        "strategy_hints": ["波动抬升，降低首仓比例并缩短信号有效期。"],
    },
    VolatilityBucket.HIGH_VOL: {
        "risk_multiplier": 1.45,
        "strategy_hints": ["高波动环境，趋势追踪需收紧止损并降低仓位。"],
    },
    VolatilityBucket.EXTREME: {
        "risk_multiplier": 1.8,
        "strategy_hints": ["极端波动，禁止激进趋势追踪，优先风控和等待确认。"],
    },
    VolatilityBucket.UNKNOWN: {
        "risk_multiplier": 1.0,
        "strategy_hints": ["波动数据不足，按保守仓位处理。"],
    },
}


@dataclass(frozen=True)
class MarketBar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0


@dataclass(frozen=True)
class SentimentComponents:
    """A-share substitutes for derivative-style sentiment components."""

    margin_balance_change: Optional[float] = None
    market_breadth: Optional[float] = None
    fear_greed_index: Optional[float] = None
    northbound_flow_z: Optional[float] = None
    market_flow_z: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RegimeState:
    market: str
    as_of: Optional[date]
    regime: MarketRegime
    volatility_bucket: VolatilityBucket
    raw_volatility_bucket: VolatilityBucket
    volatility_percentile: Optional[float]
    atr_pct: Optional[float]
    sentiment_state: SentimentState
    sentiment_score: Optional[float]
    sentiment_components: Dict[str, Any]
    wyckoff_phase: WyckoffPhase
    risk_level: str
    risk_multiplier: float
    strategy_hints: List[str]
    evidence: List[str]
    conflicts: List[str]
    data_quality: str
    confirmation: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market": self.market,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "regime": self.regime.value,
            "volatility_bucket": self.volatility_bucket.value,
            "raw_volatility_bucket": self.raw_volatility_bucket.value,
            "volatility_percentile": self.volatility_percentile,
            "atr_pct": self.atr_pct,
            "sentiment_state": self.sentiment_state.value,
            "sentiment_score": self.sentiment_score,
            "sentiment_components": self.sentiment_components,
            "wyckoff_phase": self.wyckoff_phase.value,
            "risk_level": self.risk_level,
            "risk_multiplier": self.risk_multiplier,
            "strategy_hints": self.strategy_hints,
            "evidence": self.evidence,
            "conflicts": self.conflicts,
            "data_quality": self.data_quality,
            "confirmation": self.confirmation,
        }


def coerce_bars(rows: Iterable[Dict[str, Any]]) -> List[MarketBar]:
    bars: List[MarketBar] = []
    for row in rows or []:
        try:
            row_date = _coerce_date(row.get("date") or row.get("日期"))
            if row_date is None:
                continue
            bar = MarketBar(
                date=row_date,
                open=_to_float(row.get("open") or row.get("开盘")),
                high=_to_float(row.get("high") or row.get("最高")),
                low=_to_float(row.get("low") or row.get("最低")),
                close=_to_float(row.get("close") or row.get("收盘")),
                volume=_to_float(row.get("volume") or row.get("成交量")),
                amount=_to_float(row.get("amount") or row.get("成交额")),
            )
        except Exception:
            continue
        if bar.high > 0 and bar.low > 0 and bar.close > 0:
            bars.append(bar)
    bars.sort(key=lambda item: item.date)
    return bars


def detect_market_regime(
    bars: Sequence[MarketBar],
    *,
    market: str = "cn",
    sentiment: Optional[SentimentComponents] = None,
    previous_bucket: Optional[str] = None,
    previous_regime: Optional[str] = None,
    pending_regime: Optional[str] = None,
    pending_count: int = 0,
    confirmation_bars: int = 3,
    atr_window: int = 14,
    cdf_window: int = 252,
    wyckoff_window: int = 100,
) -> RegimeState:
    bars = list(bars or [])
    evidence: List[str] = []
    conflicts: List[str] = []
    if len(bars) < max(atr_window + 2, 30):
        return _unknown_state(
            market=market,
            bars=bars,
            reason=f"历史K线不足：需要至少 {max(atr_window + 2, 30)} 根，实际 {len(bars)} 根。",
        )

    atr_values = _atr_pct_series(bars, window=atr_window)
    current_atr_pct = atr_values[-1] if atr_values else None
    sample = atr_values[-cdf_window:] if atr_values else []
    raw_bucket, percentile = _bucket_from_empirical_cdf(current_atr_pct, sample)
    bucket = _apply_bucket_damping(raw_bucket, previous_bucket)

    if bucket != raw_bucket:
        evidence.append(f"波动档位阻尼：原始 {raw_bucket.value} -> 生效 {bucket.value}。")
    if current_atr_pct is not None and percentile is not None:
        evidence.append(f"ATR%={current_atr_pct:.4f}，历史经验分位={percentile:.2%}。")

    sentiment_state, sentiment_score, sentiment_payload = _compose_sentiment(sentiment)
    if sentiment_state != SentimentState.UNKNOWN and sentiment_score is not None:
        evidence.append(f"A股情绪合成分数={sentiment_score:.2f}，状态={sentiment_state.value}。")

    wyckoff_phase, wyckoff_evidence = _detect_wyckoff_phase(bars[-wyckoff_window:])
    evidence.extend(wyckoff_evidence)

    trend = _trend_metrics(bars)
    raw_regime = _classify_regime(
        trend=trend,
        bucket=bucket,
        sentiment_state=sentiment_state,
        sentiment_score=sentiment_score,
        wyckoff_phase=wyckoff_phase,
    )
    regime, confirmation = _apply_regime_confirmation(
        raw_regime=raw_regime,
        previous_regime=previous_regime,
        pending_regime=pending_regime,
        pending_count=pending_count,
        confirmation_bars=max(1, int(confirmation_bars)),
    )

    if regime != raw_regime:
        evidence.append(
            f"Regime 持久化确认中：原始 {raw_regime.value}，当前保持 {regime.value}。"
        )

    conflicts.extend(_detect_conflicts(bucket, sentiment_state, wyckoff_phase, trend))
    meta = _VOL_BUCKET_META.get(bucket, _VOL_BUCKET_META[VolatilityBucket.UNKNOWN])
    strategy_hints = list(meta["strategy_hints"])
    strategy_hints.extend(_regime_strategy_hints(regime, wyckoff_phase, sentiment_state))
    risk_level = _risk_level(regime, bucket, sentiment_state)
    data_quality = _data_quality(len(bars), sentiment_payload)

    return RegimeState(
        market=market,
        as_of=bars[-1].date if bars else None,
        regime=regime,
        volatility_bucket=bucket,
        raw_volatility_bucket=raw_bucket,
        volatility_percentile=round(percentile, 4) if percentile is not None else None,
        atr_pct=round(current_atr_pct, 6) if current_atr_pct is not None else None,
        sentiment_state=sentiment_state,
        sentiment_score=round(sentiment_score, 4) if sentiment_score is not None else None,
        sentiment_components=sentiment_payload,
        wyckoff_phase=wyckoff_phase,
        risk_level=risk_level,
        risk_multiplier=float(meta["risk_multiplier"]),
        strategy_hints=strategy_hints,
        evidence=evidence,
        conflicts=conflicts,
        data_quality=data_quality,
        confirmation=confirmation,
    )


def _unknown_state(market: str, bars: Sequence[MarketBar], reason: str) -> RegimeState:
    return RegimeState(
        market=market,
        as_of=bars[-1].date if bars else None,
        regime=MarketRegime.UNKNOWN,
        volatility_bucket=VolatilityBucket.UNKNOWN,
        raw_volatility_bucket=VolatilityBucket.UNKNOWN,
        volatility_percentile=None,
        atr_pct=None,
        sentiment_state=SentimentState.UNKNOWN,
        sentiment_score=None,
        sentiment_components={},
        wyckoff_phase=WyckoffPhase.UNKNOWN,
        risk_level="unknown",
        risk_multiplier=1.0,
        strategy_hints=["数据不足，保守采用监控/等待。"],
        evidence=[reason],
        conflicts=[],
        data_quality="insufficient",
        confirmation={"state": "insufficient_data", "pending_count": 0, "required": 0},
    )


def _atr_pct_series(bars: Sequence[MarketBar], window: int) -> List[float]:
    true_ranges: List[float] = []
    prev_close: Optional[float] = None
    for bar in bars:
        if prev_close is None:
            true_range = bar.high - bar.low
        else:
            true_range = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        true_ranges.append(max(true_range, 0.0))
        prev_close = bar.close

    result: List[float] = []
    for idx in range(window - 1, len(true_ranges)):
        atr = mean(true_ranges[idx - window + 1 : idx + 1])
        close = bars[idx].close
        if close > 0:
            result.append(atr / close)
    return result


def _bucket_from_empirical_cdf(value: Optional[float], sample: Sequence[float]) -> tuple[VolatilityBucket, Optional[float]]:
    valid = [x for x in sample if x is not None and math.isfinite(x)]
    if value is None or not valid:
        return VolatilityBucket.UNKNOWN, None
    percentile = sum(1 for x in valid if x <= value) / len(valid)
    if percentile > 0.95:
        bucket = VolatilityBucket.EXTREME
    elif percentile > 0.85:
        bucket = VolatilityBucket.HIGH_VOL
    elif percentile > 0.70:
        bucket = VolatilityBucket.ELEVATED
    elif percentile >= 0.58:
        bucket = VolatilityBucket.NORMAL
    elif percentile >= 0.35:
        bucket = VolatilityBucket.LOW
    else:
        bucket = VolatilityBucket.VERY_LOW
    return bucket, percentile


def _apply_bucket_damping(raw_bucket: VolatilityBucket, previous_bucket: Optional[str]) -> VolatilityBucket:
    if raw_bucket not in _BUCKET_ORDER:
        return raw_bucket
    try:
        prev = VolatilityBucket(str(previous_bucket or ""))
    except ValueError:
        return raw_bucket
    if prev not in _BUCKET_ORDER:
        return raw_bucket
    raw_idx = _BUCKET_ORDER.index(raw_bucket)
    prev_idx = _BUCKET_ORDER.index(prev)
    if abs(raw_idx - prev_idx) <= 1:
        return raw_bucket
    return _BUCKET_ORDER[prev_idx + (1 if raw_idx > prev_idx else -1)]


def _compose_sentiment(sentiment: Optional[SentimentComponents]) -> tuple[SentimentState, Optional[float], Dict[str, Any]]:
    if sentiment is None:
        return SentimentState.UNKNOWN, None, {}

    components = {
        "margin_balance_change": _clip(sentiment.margin_balance_change, -1.0, 1.0),
        "market_breadth": _clip(sentiment.market_breadth, -1.0, 1.0),
        "fear_greed_index": _clip(
            None if sentiment.fear_greed_index is None else (sentiment.fear_greed_index - 50.0) / 50.0,
            -1.0,
            1.0,
        ),
        "northbound_flow_z": _clip(sentiment.northbound_flow_z, -1.0, 1.0),
        "market_flow_z": _clip(sentiment.market_flow_z, -1.0, 1.0),
    }
    weights = {
        "margin_balance_change": 0.22,
        "market_breadth": 0.24,
        "fear_greed_index": 0.20,
        "northbound_flow_z": 0.18,
        "market_flow_z": 0.16,
    }
    available = {key: value for key, value in components.items() if value is not None}
    payload = {"components": components, "raw": dict(sentiment.raw or {})}
    if not available:
        return SentimentState.UNKNOWN, None, payload
    weight_sum = sum(weights[key] for key in available)
    score = sum(available[key] * weights[key] for key in available) / weight_sum
    if score >= 0.65:
        state = SentimentState.EXTREME_GREED
    elif score >= 0.25:
        state = SentimentState.GREED
    elif score <= -0.65:
        state = SentimentState.EXTREME_FEAR
    elif score <= -0.25:
        state = SentimentState.FEAR
    else:
        state = SentimentState.NEUTRAL
    payload["available_components"] = list(available.keys())
    payload["score"] = round(score, 4)
    return state, score, payload


def _detect_wyckoff_phase(bars: Sequence[MarketBar]) -> tuple[WyckoffPhase, List[str]]:
    bars = list(bars or [])
    if len(bars) < 40:
        return WyckoffPhase.UNKNOWN, ["Wyckoff 样本不足，无法稳定识别相位。"]

    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    volumes = [max(bar.volume, 0.0) for bar in bars]
    high = max(highs)
    low = min(lows)
    width = high - low
    if width <= 0:
        return WyckoffPhase.UNKNOWN, ["价格区间宽度为 0，无法识别 Wyckoff 相位。"]

    current_pos = (closes[-1] - low) / width
    first_vol = mean(volumes[: max(1, len(volumes) // 3)])
    last_vol = mean(volumes[-max(1, len(volumes) // 3) :])
    vol_ratio = _safe_div(last_vol, first_vol)
    price_change = _safe_div(closes[-1] - closes[0], closes[0])
    ranges = [max(bar.high - bar.low, 0.0) for bar in bars]
    avg_range = mean(ranges)
    avg_volume = mean(volumes) or 1.0
    recent = bars[-10:]
    recent_range = mean(max(bar.high - bar.low, 0.0) for bar in recent)
    recent_volume = mean(max(bar.volume, 0.0) for bar in recent)
    effort_result = _safe_div(recent_volume / avg_volume, abs(price_change) + 0.01)
    spring = min(lows[-20:]) <= low * 1.03 and closes[-1] > low + width * 0.25
    upthrust = max(highs[-20:]) >= high * 0.97 and closes[-1] < high - width * 0.25

    evidence = [
        f"Wyckoff: 区间位置={current_pos:.2f}，价格变化={price_change:.2%}，后段/前段量比={vol_ratio:.2f}。",
        f"VSA: 近期振幅/均值={_safe_div(recent_range, avg_range):.2f}，努力结果比={effort_result:.2f}。",
    ]
    if spring:
        evidence.append("检测到 Spring 假跌破迹象。")
    if upthrust:
        evidence.append("检测到 Upthrust 假突破迹象。")

    if price_change > 0.08 and current_pos > 0.65:
        return WyckoffPhase.MARKUP, evidence
    if price_change < -0.08 and current_pos < 0.35:
        return WyckoffPhase.MARKDOWN, evidence
    if current_pos < 0.45 and (vol_ratio > 1.08 or spring or effort_result > 1.8):
        return WyckoffPhase.ACCUMULATION, evidence
    if current_pos > 0.55 and (vol_ratio > 1.08 or upthrust or effort_result > 1.8):
        return WyckoffPhase.DISTRIBUTION, evidence
    return WyckoffPhase.RANGE, evidence


def _trend_metrics(bars: Sequence[MarketBar]) -> Dict[str, Any]:
    closes = [bar.close for bar in bars]
    last = closes[-1]
    ma20 = mean(closes[-20:])
    ma60 = mean(closes[-60:]) if len(closes) >= 60 else mean(closes)
    ret20 = _safe_div(last - closes[-20], closes[-20]) if len(closes) >= 20 else 0.0
    ret60 = _safe_div(last - closes[-60], closes[-60]) if len(closes) >= 60 else ret20
    return {
        "last": last,
        "ma20": ma20,
        "ma60": ma60,
        "ret20": ret20,
        "ret60": ret60,
        "above_ma20": last >= ma20,
        "above_ma60": last >= ma60,
    }


def _classify_regime(
    *,
    trend: Dict[str, Any],
    bucket: VolatilityBucket,
    sentiment_state: SentimentState,
    sentiment_score: Optional[float],
    wyckoff_phase: WyckoffPhase,
) -> MarketRegime:
    if bucket == VolatilityBucket.EXTREME and trend.get("ret20", 0.0) < -0.06:
        return MarketRegime.PANIC
    if (
        bucket in {VolatilityBucket.HIGH_VOL, VolatilityBucket.EXTREME}
        and sentiment_state in {SentimentState.FEAR, SentimentState.EXTREME_FEAR}
    ):
        return MarketRegime.RISK_OFF
    if bucket in {VolatilityBucket.HIGH_VOL, VolatilityBucket.EXTREME}:
        return MarketRegime.HIGH_VOLATILITY
    if wyckoff_phase == WyckoffPhase.MARKUP or (
        trend.get("above_ma20") and trend.get("above_ma60") and trend.get("ret20", 0.0) > 0.03
    ):
        return MarketRegime.TRENDING_UP
    if wyckoff_phase == WyckoffPhase.MARKDOWN or (
        not trend.get("above_ma20") and not trend.get("above_ma60") and trend.get("ret20", 0.0) < -0.03
    ):
        return MarketRegime.TRENDING_DOWN
    return MarketRegime.RANGE_BOUND


def _apply_regime_confirmation(
    *,
    raw_regime: MarketRegime,
    previous_regime: Optional[str],
    pending_regime: Optional[str],
    pending_count: int,
    confirmation_bars: int,
) -> tuple[MarketRegime, Dict[str, Any]]:
    try:
        previous = MarketRegime(str(previous_regime or ""))
    except ValueError:
        previous = MarketRegime.UNKNOWN
    if previous in {MarketRegime.UNKNOWN, raw_regime}:
        return raw_regime, {
            "state": "confirmed",
            "raw_regime": raw_regime.value,
            "previous_regime": previous.value,
            "pending_regime": None,
            "pending_count": 0,
            "required": confirmation_bars,
        }

    next_count = pending_count + 1 if pending_regime == raw_regime.value else 1
    if next_count >= confirmation_bars:
        return raw_regime, {
            "state": "switched",
            "raw_regime": raw_regime.value,
            "previous_regime": previous.value,
            "pending_regime": None,
            "pending_count": 0,
            "required": confirmation_bars,
        }
    return previous, {
        "state": "pending",
        "raw_regime": raw_regime.value,
        "previous_regime": previous.value,
        "pending_regime": raw_regime.value,
        "pending_count": next_count,
        "required": confirmation_bars,
    }


def _detect_conflicts(
    bucket: VolatilityBucket,
    sentiment_state: SentimentState,
    wyckoff_phase: WyckoffPhase,
    trend: Dict[str, Any],
) -> List[str]:
    conflicts: List[str] = []
    if bucket == VolatilityBucket.EXTREME and sentiment_state == SentimentState.EXTREME_GREED:
        conflicts.append("极端波动与极端贪婪并存，存在冲高回落或拥挤交易风险。")
    if wyckoff_phase == WyckoffPhase.DISTRIBUTION and trend.get("above_ma20"):
        conflicts.append("价格仍在短均线上方，但 Wyckoff 显示派发迹象。")
    if wyckoff_phase == WyckoffPhase.ACCUMULATION and not trend.get("above_ma20"):
        conflicts.append("可能处于吸筹区，但趋势尚未修复。")
    return conflicts


def _regime_strategy_hints(
    regime: MarketRegime,
    wyckoff_phase: WyckoffPhase,
    sentiment_state: SentimentState,
) -> List[str]:
    hints: List[str] = []
    if regime == MarketRegime.RISK_OFF:
        hints.append("risk_off 下优先降低风险暴露，开仓需等待市场确认。")
    elif regime == MarketRegime.PANIC:
        hints.append("panic 下禁止追涨杀跌，优先处理止损和流动性。")
    elif regime == MarketRegime.TRENDING_UP:
        hints.append("趋势向上时可接受回踩确认后的顺势策略。")
    elif regime == MarketRegime.TRENDING_DOWN:
        hints.append("趋势向下时反弹优先视为减仓或观察机会。")
    else:
        hints.append("震荡环境下优先箱体上下沿和低吸高抛，避免中位追价。")
    if wyckoff_phase == WyckoffPhase.DISTRIBUTION:
        hints.append("派发相位下个股突破信号需要更高成交额和资金确认。")
    if sentiment_state == SentimentState.EXTREME_GREED:
        hints.append("极端贪婪时加入逆向交易提醒，避免情绪高位追入。")
    if sentiment_state == SentimentState.EXTREME_FEAR:
        hints.append("极端恐惧时不直接抄底，等待恐慌释放后的二次确认。")
    return hints


def _risk_level(regime: MarketRegime, bucket: VolatilityBucket, sentiment_state: SentimentState) -> str:
    if regime in {MarketRegime.PANIC, MarketRegime.RISK_OFF} or bucket == VolatilityBucket.EXTREME:
        return "high"
    if bucket in {VolatilityBucket.ELEVATED, VolatilityBucket.HIGH_VOL}:
        return "medium_high"
    if sentiment_state in {SentimentState.EXTREME_GREED, SentimentState.EXTREME_FEAR}:
        return "medium_high"
    if regime == MarketRegime.UNKNOWN:
        return "unknown"
    return "medium"


def _data_quality(bar_count: int, sentiment_payload: Dict[str, Any]) -> str:
    sentiment_count = len(sentiment_payload.get("available_components") or [])
    if bar_count >= 120 and sentiment_count >= 3:
        return "sufficient"
    if bar_count >= 60:
        return "limited"
    return "insufficient"


def _coerce_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    if hasattr(value, "date"):
        try:
            coerced = value.date()
            return coerced if isinstance(coerced, date) else None
        except Exception:
            return None
    return None


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except Exception:
        return default


def _clip(value: Optional[float], low: float, high: float) -> Optional[float]:
    if value is None:
        return None
    try:
        numeric = float(value)
    except Exception:
        return None
    if math.isnan(numeric) or math.isinf(numeric):
        return None
    return max(low, min(high, numeric))


def _safe_div(num: float, den: float) -> float:
    return float(num) / float(den) if den else 0.0
