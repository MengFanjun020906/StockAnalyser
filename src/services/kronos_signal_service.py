# -*- coding: utf-8 -*-
"""Kronos forecast-derived K-line pattern signal service."""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pandas as pd

from src.agent.structure import analyze_price_structure, coerce_price_bars
from src.config import Config, get_config
from src.services.kronos_data_adapter import (
    MIN_DAILY_BARS,
    KronosDataAdapter,
    KronosPreparedData,
    bars_to_records,
    forecast_to_records,
    normalize_a_share_code,
)
from src.services.kronos_inference_service import KronosInferenceResult, KronosInferenceService
from src.services.kline_pattern_detector import detect_kline_patterns

logger = logging.getLogger(__name__)

PATTERN_LABELS = {
    "bottom_reversal": "底部反转",
    "range_breakout": "箱体突破",
    "breakout_pullback": "突破回踩",
    "volume_price_repair": "量价修复",
    "top_reversal_risk": "顶部反转风险",
    "double_bottom": "W底",
    "double_top": "M顶",
    "head_shoulders_top": "头肩顶",
    "inverse_head_shoulders_bottom": "头肩底",
    "triangle_consolidation": "三角收敛",
}


class KronosSignalService:
    """Analyze A-share daily K-line patterns with Kronos forecasts."""

    def __init__(
        self,
        *,
        config: Optional[Config] = None,
        data_adapter: Optional[KronosDataAdapter] = None,
        inference_service: Optional[Any] = None,
        repository: Optional[Any] = None,
    ) -> None:
        self.config = config or get_config()
        self.data_adapter = data_adapter or KronosDataAdapter()
        self.inference_service = inference_service or KronosInferenceService(self.config)
        self.repository = repository

    def status(self) -> Dict[str, Any]:
        status = _call_status(self.inference_service)
        return {
            "status": "ok",
            "enabled": bool(status.get("enabled")),
            "market_scope": ["cn"],
            "frequency_scope": ["1d"],
            **status,
        }

    def analyze(
        self,
        stock_code: str,
        *,
        frequency: str = "1d",
        lookback: Optional[int] = None,
        pred_len: Optional[int] = None,
        target_date: Optional[date] = None,
        fallback_to_network: bool = True,
        persist: Optional[bool] = None,
    ) -> Dict[str, Any]:
        code = normalize_a_share_code(stock_code)
        if not code:
            return {
                "status": "unsupported_market",
                "code": str(stock_code or "").strip().upper(),
                "market": "unsupported",
                "frequency": frequency,
                "reason": "kronos_daily_v1_supports_a_share_only",
                "pattern_instances": [],
                "bars": [],
                "forecast": [],
                "signal": {},
                "forecast_summary": {},
                "data_quality": {"status": "unsupported"},
                "source_chain": [],
                "diagnostics": {},
            }
        if frequency not in {"1d", "daily"}:
            return {
                "status": "unsupported_frequency",
                "code": code,
                "market": "cn",
                "frequency": frequency,
                "reason": "kronos_v1_supports_daily_frequency_only",
                "pattern_instances": [],
                "bars": [],
                "forecast": [],
                "signal": {},
                "forecast_summary": {},
                "data_quality": {"status": "unsupported_frequency"},
                "source_chain": [],
                "diagnostics": {},
            }

        if not _is_enabled(self.inference_service):
            return {
                "status": "unavailable",
                "code": code,
                "market": "cn",
                "frequency": "1d",
                "reason": "model_disabled",
                "pattern_instances": [],
                "bars": [],
                "forecast": [],
                "signal": {},
                "forecast_summary": {},
                "data_quality": {"status": "not_loaded"},
                "source_chain": ["kronos:disabled"],
                "diagnostics": _call_status(self.inference_service),
            }

        effective_lookback = int(lookback or self.config.kronos_default_lookback_daily)
        effective_pred_len = int(pred_len or self.config.kronos_default_pred_len_daily)
        prepared = self.data_adapter.prepare_daily(
            code,
            lookback=effective_lookback,
            pred_len=effective_pred_len,
            target_date=target_date,
            fallback_to_network=fallback_to_network,
        )
        if prepared.data_quality.get("status") != "sufficient":
            return self._base_result(
                prepared,
                status="insufficient_data",
                reason="insufficient_daily_history",
            )

        inference = self.inference_service.predict_daily(prepared, pred_len=effective_pred_len)
        if inference.status != "ok" or inference.forecast_df is None or inference.forecast_df.empty:
            result = self._base_result(
                prepared,
                status="unavailable" if inference.status == "unavailable" else "error",
                reason=inference.error or inference.status,
            )
            result["diagnostics"] = {**result["diagnostics"], **inference.diagnostics}
            result["model_profile"] = inference.model_profile
            return result

        result = self._build_success_result(prepared, inference)
        should_persist = self.config.kronos_cache_signals if persist is None else bool(persist)
        if should_persist:
            self._persist_result(result)
        return result

    def _base_result(self, prepared: KronosPreparedData, *, status: str, reason: str) -> Dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "code": prepared.code,
            "market": prepared.market,
            "frequency": prepared.frequency,
            "run_id": None,
            "as_of": prepared.as_of,
            "bars": bars_to_records(prepared.bars_df),
            "forecast": [],
            "signal": {},
            "forecast_summary": {},
            "pattern_instances": [],
            "data_quality": prepared.data_quality,
            "source_chain": [f"history_loader:{prepared.source}"],
            "diagnostics": {},
        }

    def _build_success_result(self, prepared: KronosPreparedData, inference: KronosInferenceResult) -> Dict[str, Any]:
        forecast_summary = _summarize_forecast(prepared.bars_df, inference.forecast_df)
        signal = _classify_signal(forecast_summary)
        structure_context = _analyze_structure(prepared.bars_df)
        patterns = _detect_pattern_instances(
            prepared,
            signal=signal,
            forecast_summary=forecast_summary,
            structure_context=structure_context,
        )
        run_id = f"kronos-{prepared.code}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
        source_chain = [
            f"history_loader:{prepared.source}",
            "kline_pattern_detector",
            f"kronos:{inference.model_profile.get('model_name') or 'unknown'}",
            "price_structure",
        ]
        return {
            "status": "ok",
            "code": prepared.code,
            "market": prepared.market,
            "frequency": prepared.frequency,
            "run_id": run_id,
            "as_of": prepared.as_of,
            "bars": bars_to_records(prepared.bars_df),
            "forecast": forecast_to_records(inference.forecast_df),
            "signal": signal,
            "forecast_summary": forecast_summary,
            "pattern_instances": patterns,
            "structure_context": structure_context,
            "data_quality": prepared.data_quality,
            "source_chain": source_chain,
            "model_profile": inference.model_profile,
            "diagnostics": inference.diagnostics,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _persist_result(self, result: Dict[str, Any]) -> None:
        try:
            repository = self.repository
            if repository is None:
                from src.repositories.kronos_repo import KronosRepository

                repository = KronosRepository()
            repository.save_analysis_result(result)
        except Exception as exc:  # pragma: no cover - persistence must not mask analysis
            logger.warning("Persisting Kronos result failed: %s", exc, exc_info=True)
            result.setdefault("diagnostics", {})["persist_error"] = str(exc)


def _call_status(inference_service: Any) -> Dict[str, Any]:
    status_fn = getattr(inference_service, "status", None)
    if callable(status_fn):
        status = status_fn()
        if isinstance(status, dict):
            return status
    return {"enabled": True}


def _is_enabled(inference_service: Any) -> bool:
    enabled_fn = getattr(inference_service, "is_enabled", None)
    if callable(enabled_fn):
        return bool(enabled_fn())
    return bool(_call_status(inference_service).get("enabled", True))


def _summarize_forecast(history: pd.DataFrame, forecast: pd.DataFrame) -> Dict[str, Any]:
    last_close = float(history["close"].iloc[-1])
    close = pd.to_numeric(forecast["close"], errors="coerce").dropna()
    high = pd.to_numeric(forecast.get("high", close), errors="coerce").dropna()
    low = pd.to_numeric(forecast.get("low", close), errors="coerce").dropna()
    final_close = float(close.iloc[-1]) if not close.empty else last_close
    forecast_high = float(high.max()) if not high.empty else final_close
    forecast_low = float(low.min()) if not low.empty else final_close
    expected_return_pct = (final_close / last_close - 1.0) * 100.0 if last_close else 0.0
    max_upside_pct = (forecast_high / last_close - 1.0) * 100.0 if last_close else 0.0
    max_drawdown_pct = (forecast_low / last_close - 1.0) * 100.0 if last_close else 0.0
    downside = abs(min(max_drawdown_pct, 0.0))
    risk_reward = max_upside_pct / downside if downside > 0.05 else 99.0
    returns = close.pct_change().dropna()
    positive_steps = int((returns > 0).sum()) if not returns.empty else 0
    trend_consistency = positive_steps / max(len(returns), 1) if not returns.empty else 0.0
    return {
        "last_close": round(last_close, 4),
        "final_close": round(final_close, 4),
        "forecast_high": round(forecast_high, 4),
        "forecast_low": round(forecast_low, 4),
        "expected_return_pct": round(expected_return_pct, 4),
        "max_upside_pct": round(max_upside_pct, 4),
        "max_drawdown_pct": round(max_drawdown_pct, 4),
        "risk_reward": round(min(risk_reward, 99.0), 4),
        "trend_consistency": round(trend_consistency, 4),
        "horizon_bars": int(len(forecast)),
    }


def _classify_signal(summary: Dict[str, Any]) -> Dict[str, Any]:
    expected = float(summary.get("expected_return_pct") or 0.0)
    upside = float(summary.get("max_upside_pct") or 0.0)
    drawdown = float(summary.get("max_drawdown_pct") or 0.0)
    consistency = float(summary.get("trend_consistency") or 0.0)
    if expected >= 2.0 and upside >= abs(min(drawdown, 0.0)):
        bias = "bullish"
    elif expected <= -2.0 or abs(min(drawdown, 0.0)) > max(upside, 0.0) * 1.25:
        bias = "bearish"
    else:
        bias = "neutral"

    confidence = 0.42 + min(abs(expected), 8.0) / 20.0 + consistency * 0.16
    if bias == "neutral":
        confidence = min(confidence, 0.58)
    return {
        "bias": bias,
        "confidence": round(max(0.2, min(confidence, 0.95)), 4),
        "expected_return_pct": summary.get("expected_return_pct"),
        "risk_reward": summary.get("risk_reward"),
        "horizon_bars": summary.get("horizon_bars"),
        "action_bias": {
            "bullish": "watch_bullish_pattern",
            "bearish": "risk_review",
            "neutral": "watch_only",
        }[bias],
    }


def _analyze_structure(history: pd.DataFrame) -> Dict[str, Any]:
    try:
        bars = coerce_price_bars(bars_to_records(history))
        result = analyze_price_structure(bars, min_bars=MIN_DAILY_BARS, include_raw=False)
        return {
            "status": result.get("status"),
            "data_quality": result.get("data_quality"),
            "bar_count": result.get("bar_count"),
            "chan": (result.get("chan") or {}).get("structure_summary"),
            "smc": {
                "bos": (result.get("smc") or {}).get("bos"),
                "choch": (result.get("smc") or {}).get("choch"),
                "structure_summary": (result.get("smc") or {}).get("structure_summary"),
            },
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def _detect_pattern_instances(
    prepared: KronosPreparedData,
    *,
    signal: Dict[str, Any],
    forecast_summary: Dict[str, Any],
    structure_context: Dict[str, Any],
) -> List[Dict[str, Any]]:
    history = prepared.bars_df.reset_index(drop=True)
    if len(history) < MIN_DAILY_BARS:
        return []
    latest = history.iloc[-1]
    recent = history.tail(20)
    prior = history.iloc[-45:-20] if len(history) >= 45 else history.iloc[:-20]
    patterns: List[Dict[str, Any]] = []
    bias = str(signal.get("bias") or "neutral")
    confidence = float(signal.get("confidence") or 0.0)

    for pattern in detect_kline_patterns(history, code=prepared.code, max_patterns=4):
        patterns.append(
            _attach_kronos_confirmation(
                pattern,
                signal=signal,
                forecast_summary=forecast_summary,
                structure_context=structure_context,
            )
        )

    if bias == "bullish":
        low_idx = int(recent["low"].astype(float).idxmin())
        latest_close = float(latest["close"])
        recent_low = float(history.loc[low_idx, "low"])
        if latest_close >= recent_low * 1.02:
            patterns.append(
                _pattern(
                    prepared.code,
                    "bottom_reversal",
                    "bullish",
                    confidence,
                    history.loc[low_idx:],
                    forecast_summary,
                    structure_context,
                    "Kronos forecast is positive after a recent daily swing low.",
                )
            )
        if not prior.empty:
            prior_high = float(prior["high"].astype(float).max())
            if latest_close >= prior_high * 0.995:
                patterns.append(
                    _pattern(
                        prepared.code,
                        "range_breakout",
                        "bullish",
                        min(confidence + 0.06, 0.96),
                        history.tail(35),
                        forecast_summary,
                        structure_context,
                        "Latest close is testing or exceeding the previous range high.",
                    )
                )
        vol_ma20 = float(recent["volume"].astype(float).mean()) if "volume" in recent else 0.0
        latest_volume = float(latest.get("volume") or 0.0)
        if latest_volume >= vol_ma20 * 1.05 and latest_close >= float(recent["close"].astype(float).median()):
            patterns.append(
                _pattern(
                    prepared.code,
                    "volume_price_repair",
                    "bullish",
                    min(confidence + 0.03, 0.95),
                    recent,
                    forecast_summary,
                    structure_context,
                    "Volume is expanding while price repairs above the recent median close.",
                )
            )
    elif bias == "bearish":
        high_idx = int(recent["high"].astype(float).idxmax())
        patterns.append(
            _pattern(
                prepared.code,
                "top_reversal_risk",
                "bearish",
                confidence,
                history.loc[high_idx:],
                forecast_summary,
                structure_context,
                "Kronos forecast shows downside risk after a recent swing high.",
            )
        )

    if bias == "bullish" and not patterns:
        patterns.append(
            _pattern(
                prepared.code,
                "breakout_pullback",
                "bullish",
                confidence,
                recent,
                forecast_summary,
                structure_context,
                "Kronos forecast is bullish but historical breakout evidence is limited.",
            )
        )
    return _dedupe_pattern_dicts(patterns)[:5]


def _attach_kronos_confirmation(
    pattern: Dict[str, Any],
    *,
    signal: Dict[str, Any],
    forecast_summary: Dict[str, Any],
    structure_context: Dict[str, Any],
) -> Dict[str, Any]:
    direction = str(pattern.get("direction") or "neutral")
    bias = str(signal.get("bias") or "neutral")
    if direction == "neutral":
        confirmation = "observation_only"
    elif bias == direction:
        confirmation = "kronos_confirmed"
    elif bias == "neutral":
        confirmation = "kronos_unconfirmed"
    else:
        confirmation = "kronos_divergence"

    adjusted = dict(pattern)
    base_confidence = float(adjusted.get("confidence") or 0.0)
    if confirmation == "kronos_confirmed":
        base_confidence += 0.04
    elif confirmation == "kronos_divergence":
        base_confidence -= 0.05
    adjusted["confidence"] = round(max(0.0, min(base_confidence, 1.0)), 4)
    evidence = dict(adjusted.get("evidence") or {})
    evidence.update(
        {
            "kronos_confirmation": confirmation,
            "kronos_bias": bias,
            "forecast_summary": forecast_summary,
            "structure_context": structure_context,
        }
    )
    adjusted["evidence"] = evidence
    return adjusted


def _dedupe_pattern_dicts(patterns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    selected: Dict[str, Dict[str, Any]] = {}
    for pattern in patterns:
        key = "|".join(
            [
                str(pattern.get("pattern_type") or ""),
                str(pattern.get("start_time") or ""),
                str(pattern.get("end_time") or ""),
            ]
        )
        current = selected.get(key)
        if current is None or float(pattern.get("confidence") or 0.0) > float(current.get("confidence") or 0.0):
            selected[key] = pattern
    return sorted(selected.values(), key=lambda item: float(item.get("confidence") or 0.0), reverse=True)


def _pattern(
    code: str,
    pattern_type: str,
    direction: str,
    confidence: float,
    window: pd.DataFrame,
    forecast_summary: Dict[str, Any],
    structure_context: Dict[str, Any],
    summary: str,
) -> Dict[str, Any]:
    clean_window = window.reset_index(drop=True)
    first = clean_window.iloc[0]
    last = clean_window.iloc[-1]
    start_time = pd.Timestamp(first["timestamp"]).date().isoformat()
    end_time = pd.Timestamp(last["timestamp"]).date().isoformat()
    price_low = round(float(clean_window["low"].astype(float).min()), 4)
    price_high = round(float(clean_window["high"].astype(float).max()), 4)
    pattern_id = _pattern_id(code, pattern_type, start_time, end_time, price_low, price_high)
    return {
        "pattern_id": pattern_id,
        "pattern_type": pattern_type,
        "label": PATTERN_LABELS.get(pattern_type, pattern_type),
        "direction": direction,
        "confidence": round(max(0.0, min(float(confidence), 1.0)), 4),
        "start_time": start_time,
        "end_time": end_time,
        "price_low": price_low,
        "price_high": price_high,
        "red_box": {"x0": start_time, "x1": end_time, "y0": price_low, "y1": price_high},
        "summary": summary,
        "evidence": {
            "detector": "kronos_forecast_heuristic_v1",
            "kronos_confirmation": "kronos_derived",
            "forecast_summary": forecast_summary,
            "structure_context": structure_context,
            "window_bars": int(len(clean_window)),
        },
    }


def _pattern_id(code: str, pattern_type: str, start_time: str, end_time: str, price_low: float, price_high: float) -> str:
    raw = f"{code}|{pattern_type}|{start_time}|{end_time}|{price_low}|{price_high}"
    return "kpat-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
