# -*- coding: utf-8 -*-
"""AlphaSift-style configurable candidate provider.

This provider intentionally absorbs AlphaSift's L1 idea (YAML strategies,
hard filters, factor scoring, structured output) without making the production
Agent depend on a cloned sub-repository.  It uses the same local daily-bar
SQLite cache as the Sequoia provider, so both recall paths can be merged.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd
import yaml

from src.agent.candidate_providers.sequoia_provider import DEFAULT_SEQUOIA_DB_PATH
from src.data.stock_index_loader import get_index_stock_name
from src.data.stock_mapping import STOCK_NAME_MAP, is_meaningful_stock_name

logger = logging.getLogger(__name__)

DEFAULT_ALPHASIFT_STRATEGY_DIR = "alphasift/alphasift/strategies"
_REQUIRED_COLUMNS = {"symbol", "date", "open", "high", "low", "close", "volume", "turnover"}


@dataclass(frozen=True)
class AlphaSiftStrategy:
    name: str
    display_name: str
    description: str
    category: str
    tags: List[str]
    hard_filters: Dict[str, Any] = field(default_factory=dict)
    factor_weights: Dict[str, float] = field(default_factory=dict)
    ranking_hints: str = ""
    max_output: int = 5


class AlphaSiftCandidateProvider:
    """Run AlphaSift-style YAML screening on a local OHLCV cache."""

    def __init__(self, db_path: Optional[str] = None, strategies_dir: Optional[str] = None) -> None:
        self.db_path = str(db_path or _default_db_path())
        self.strategies_dir = str(strategies_dir or _default_strategy_dir())

    def discover(
        self,
        *,
        limit: int = 8,
        strategy_names: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        effective_limit = max(1, min(int(limit or 8), 50))
        diagnostics: List[Dict[str, Any]] = []

        validation_error = self._validate_db()
        if validation_error:
            return {
                "status": "unavailable",
                "provider": "alphasift",
                "db_path": self.db_path,
                "strategies_dir": self.strategies_dir,
                "candidates": [],
                "diagnostics": [{"source": "alphasift_db", "status": "unavailable", "error": validation_error}],
                "error": validation_error,
            }

        strategies, strategy_error = self._load_strategies(strategy_names)
        if strategy_error:
            return {
                "status": "unavailable",
                "provider": "alphasift",
                "db_path": self.db_path,
                "strategies_dir": self.strategies_dir,
                "candidates": [],
                "diagnostics": [{"source": "alphasift_strategies", "status": "unavailable", "error": strategy_error}],
                "error": strategy_error,
            }
        if not strategies:
            return {
                "status": "empty",
                "provider": "alphasift",
                "db_path": self.db_path,
                "strategies_dir": self.strategies_dir,
                "strategy_names": [],
                "candidates": [],
                "diagnostics": [{"source": "alphasift_strategies", "status": "empty", "reason": "No enabled AlphaSift strategies matched request"}],
            }

        try:
            bars = self._load_bars()
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.warning("AlphaSift candidate DB load failed: %s", message)
            return {
                "status": "failed",
                "provider": "alphasift",
                "db_path": self.db_path,
                "strategies_dir": self.strategies_dir,
                "candidates": [],
                "diagnostics": [{"source": "stock_daily", "status": "failed", "error": message}],
                "error": message,
            }
        if bars.empty:
            return {
                "status": "empty",
                "provider": "alphasift",
                "db_path": self.db_path,
                "strategies_dir": self.strategies_dir,
                "candidates": [],
                "diagnostics": [{"source": "stock_daily", "status": "empty"}],
            }

        features = _build_latest_features(bars)
        candidates: List[Dict[str, Any]] = []
        for strategy in strategies:
            selected = _run_strategy(strategy, features)
            diagnostics.append({
                "source": f"alphasift:{strategy.name}",
                "status": "ok",
                "count": len(selected),
                "category": strategy.category,
            })
            candidates.extend(selected)

        merged = _merge_candidates(candidates)
        merged.sort(
            key=lambda item: (
                float(item.get("signal_score") or 0),
                len(item.get("matched_strategies") or []),
                str(item.get("code") or ""),
            ),
            reverse=True,
        )
        selected = merged[:effective_limit]
        for item in selected:
            item["reason_dimensions"] = _candidate_reason_dimensions(item)
        return {
            "status": "ok" if selected else "empty",
            "provider": "alphasift",
            "db_path": self.db_path,
            "strategies_dir": self.strategies_dir,
            "latest_date": str(bars["date"].max().date()) if not bars.empty else None,
            "strategy_names": [strategy.name for strategy in strategies],
            "candidate_count": len(selected),
            "candidates": selected,
            "diagnostics": diagnostics,
        }

    def _validate_db(self) -> Optional[str]:
        path = Path(self.db_path).expanduser()
        if not path.exists():
            return f"AlphaSift candidate DB not found: {path}"
        try:
            with sqlite3.connect(str(path)) as conn:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='stock_daily'"
                ).fetchone()
                if not row:
                    return "stock_daily table not found"
                columns = {item[1] for item in conn.execute("PRAGMA table_info(stock_daily)").fetchall()}
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        missing = sorted(_REQUIRED_COLUMNS - columns)
        if missing:
            return f"stock_daily missing columns: {', '.join(missing)}"
        return None

    def _load_bars(self) -> pd.DataFrame:
        with sqlite3.connect(str(Path(self.db_path).expanduser())) as conn:
            df = pd.read_sql(
                "SELECT symbol, date, open, high, low, close, volume, turnover FROM stock_daily",
                conn,
            )
        if df.empty:
            return df
        df["symbol"] = df["symbol"].astype(str).str.strip()
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        for col in ("open", "high", "low", "close", "volume", "turnover"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["symbol", "date", "open", "high", "low", "close"])
        df = df[df["symbol"].str.fullmatch(r"\d{6}", na=False)]
        df = df[df["volume"].fillna(0) > 0]
        return df.sort_values(["symbol", "date"]).reset_index(drop=True)

    def _load_strategies(self, strategy_names: Optional[Sequence[str]]) -> tuple[List[AlphaSiftStrategy], Optional[str]]:
        path = Path(self.strategies_dir).expanduser()
        if not path.exists() or not path.is_dir():
            return [], f"AlphaSift strategy dir not found: {path}"
        wanted = _normalize_strategy_names(strategy_names)
        wanted_set = set(wanted or [])
        strategies: List[AlphaSiftStrategy] = []
        errors: List[str] = []
        for file_path in sorted(path.glob("*.yaml")):
            try:
                strategy = _load_strategy_file(file_path)
            except Exception as exc:
                errors.append(f"{file_path.name}: {exc}")
                continue
            if wanted is not None and strategy.name.strip().lower() not in wanted_set:
                continue
            strategies.append(strategy)
        if errors and not strategies:
            return [], "; ".join(errors[:5])
        return strategies, None


def _default_db_path() -> str:
    return os.getenv("ALPHASIFT_CANDIDATE_DB_PATH") or os.getenv("SEQUOIA_CANDIDATE_DB_PATH") or DEFAULT_SEQUOIA_DB_PATH


def _default_strategy_dir() -> str:
    return os.getenv("ALPHASIFT_STRATEGY_DIR") or DEFAULT_ALPHASIFT_STRATEGY_DIR


def _normalize_strategy_names(strategy_names: Optional[Sequence[str]]) -> Optional[List[str]]:
    raw = [str(item).strip().lower() for item in (strategy_names or []) if str(item or "").strip()]
    if not raw or "all" in raw:
        return None
    aliases = {
        "breakout": "volume_breakout",
        "value": "quality_value",
        "dual_low_value": "dual_low",
        "momentum": "momentum_quality",
        "pullback": "shrink_pullback",
        "reversal": "oversold_reversal",
        "heat": "capital_heat",
        "balanced": "balanced_alpha",
    }
    result: List[str] = []
    for name in raw:
        canonical = aliases.get(name, name)
        if canonical not in result:
            result.append(canonical)
    return result


def _display_stock_name(code: Any, current_name: Any = None) -> str:
    code_text = str(code or "").strip()
    current_text = str(current_name or "").strip()
    if is_meaningful_stock_name(current_text, code_text):
        return current_text
    for name in (STOCK_NAME_MAP.get(code_text), get_index_stock_name(code_text)):
        if is_meaningful_stock_name(name, code_text):
            return str(name)
    return code_text


def _load_strategy_file(path: Path) -> AlphaSiftStrategy:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    screening = data.get("screening") if isinstance(data, dict) else {}
    if not isinstance(screening, dict) or not screening.get("enabled", False):
        raise ValueError("strategy disabled or missing screening section")
    hard_filters = screening.get("hard_filters") or {}
    factor_weights = screening.get("factor_weights") or {}
    return AlphaSiftStrategy(
        name=str(data.get("name") or path.stem),
        display_name=str(data.get("display_name") or data.get("name") or path.stem),
        description=str(data.get("description") or ""),
        category=str(data.get("category") or "framework"),
        tags=[str(item) for item in (data.get("tags") or [])],
        hard_filters=hard_filters if isinstance(hard_filters, dict) else {},
        factor_weights={str(k): float(v) for k, v in factor_weights.items()} if isinstance(factor_weights, dict) else {},
        ranking_hints=str(screening.get("ranking_hints") or ""),
        max_output=int(screening.get("max_output") or 5),
    )


def _build_latest_features(bars: pd.DataFrame) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for symbol, group in bars.groupby("symbol", sort=False):
        df = group.sort_values("date").reset_index(drop=True)
        if len(df) < 20:
            continue
        rows.append(_feature_row(str(symbol), df))
    return pd.DataFrame(rows)


def _feature_row(symbol: str, df: pd.DataFrame) -> Dict[str, Any]:
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    close = float(last["close"])
    prev_close = float(prev["close"]) if float(prev["close"]) else close
    amount = float(last.get("turnover") or 0)
    volume = float(last.get("volume") or 0)
    ma5 = df["close"].rolling(5).mean().iloc[-1]
    ma20 = df["close"].rolling(20).mean().iloc[-1]
    ma60 = df["close"].rolling(60).mean().iloc[-1] if len(df) >= 60 else pd.NA
    vol_ma20 = df["volume"].rolling(20).mean().iloc[-1]
    high20 = df["high"].shift(1).rolling(20).max().iloc[-1]
    low20 = df["low"].rolling(20).min().iloc[-1]
    high20_incl = df["high"].rolling(20).max().iloc[-1]
    close60 = df["close"].iloc[-61] if len(df) >= 61 else pd.NA
    change_pct = (close - prev_close) / prev_close * 100 if prev_close else 0.0
    change_60d = (close - close60) / close60 * 100 if pd.notna(close60) and close60 else pd.NA
    breakout_20d_pct = (close - high20) / high20 * 100 if pd.notna(high20) and high20 else pd.NA
    range_20d_pct = (high20_incl - low20) / low20 * 100 if pd.notna(low20) and low20 else pd.NA
    volume_ratio_20d = volume / vol_ma20 if pd.notna(vol_ma20) and vol_ma20 else pd.NA
    body_pct = abs(float(last["close"]) - float(last["open"])) / max(float(last["high"]) - float(last["low"]), 1e-9)
    pullback_to_ma20_pct = (close - ma20) / ma20 * 100 if pd.notna(ma20) and ma20 else pd.NA
    consolidation_days = _consolidation_days_20d(df)
    macd_status = _macd_status(df["close"])
    rsi_value = _rsi(df["close"], 12)
    rsi_status = "oversold" if rsi_value < 30 else "overbought" if rsi_value > 70 else "neutral"
    boll_mid, boll_upper, boll_lower, boll_bandwidth, boll_position = _bollinger_metrics(df["close"])
    signal_score = _signal_score(change_pct, change_60d, volume_ratio_20d, ma5, ma20, ma60, macd_status)
    return {
        "code": symbol,
        "name": _display_stock_name(symbol),
        "date": str(pd.to_datetime(last["date"]).date()),
        "price": close,
        "open": float(last["open"]),
        "high": float(last["high"]),
        "low": float(last["low"]),
        "amount": amount,
        "volume": volume,
        "change_pct": change_pct,
        "turnover_rate": _turnover_proxy(volume),
        "volume_ratio": volume_ratio_20d,
        "change_60d": change_60d,
        "ma_bullish": bool(pd.notna(ma5) and pd.notna(ma20) and close > ma5 > ma20),
        "price_above_ma20": bool(pd.notna(ma20) and close > ma20),
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "signal_score": signal_score,
        "macd_status": macd_status,
        "rsi_value": rsi_value,
        "rsi_status": rsi_status,
        "boll_mid": boll_mid,
        "boll_upper": boll_upper,
        "boll_lower": boll_lower,
        "boll_bandwidth": boll_bandwidth,
        "boll_position": boll_position,
        "breakout_20d_pct": breakout_20d_pct,
        "range_20d_pct": range_20d_pct,
        "volume_ratio_20d": volume_ratio_20d,
        "body_pct": body_pct,
        "pullback_to_ma20_pct": pullback_to_ma20_pct,
        "consolidation_days_20d": consolidation_days,
    }


def _consolidation_days_20d(df: pd.DataFrame) -> int:
    tail = df.tail(20)
    if tail.empty:
        return 0
    median = float(tail["close"].median())
    if not median:
        return 0
    return int((((tail["high"] - tail["low"]) / median * 100) <= 6).sum())


def _macd_status(close: pd.Series) -> str:
    if len(close) < 35:
        return "neutral"
    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=9, adjust=False).mean()
    latest = float(dif.iloc[-1] - dea.iloc[-1])
    prev = float(dif.iloc[-2] - dea.iloc[-2])
    if latest > 0 and latest >= prev:
        return "bullish"
    if latest < 0 and latest <= prev:
        return "bearish"
    return "neutral"


def _rsi(close: pd.Series, period: int) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    value = rsi.iloc[-1]
    return float(value) if pd.notna(value) else 50.0


def _bollinger_metrics(close: pd.Series, period: int = 20, width: float = 2.0) -> tuple[Any, Any, Any, Any, str]:
    if len(close) < period:
        return pd.NA, pd.NA, pd.NA, pd.NA, "unknown"
    mid = close.rolling(period).mean().iloc[-1]
    std = close.rolling(period).std(ddof=0).iloc[-1]
    if pd.isna(mid) or pd.isna(std):
        return pd.NA, pd.NA, pd.NA, pd.NA, "unknown"
    upper = mid + width * std
    lower = mid - width * std
    last = close.iloc[-1]
    bandwidth = (upper - lower) / mid * 100 if mid else pd.NA
    if last >= upper:
        position = "above_upper"
    elif last <= lower:
        position = "below_lower"
    elif last >= mid:
        position = "upper_half"
    else:
        position = "lower_half"
    return mid, upper, lower, bandwidth, position


def _signal_score(
    change_pct: float,
    change_60d: Any,
    volume_ratio: Any,
    ma5: Any,
    ma20: Any,
    ma60: Any,
    macd_status: str,
) -> float:
    score = 50.0
    score += max(min(change_pct * 2.0, 12), -12)
    if pd.notna(change_60d):
        score += max(min(float(change_60d) * 0.25, 15), -15)
    if pd.notna(volume_ratio):
        score += max(min((float(volume_ratio) - 1.0) * 8, 12), -6)
    if pd.notna(ma5) and pd.notna(ma20) and ma5 > ma20:
        score += 6
    if pd.notna(ma20) and pd.notna(ma60) and ma20 > ma60:
        score += 6
    if macd_status == "bullish":
        score += 6
    elif macd_status == "bearish":
        score -= 8
    return round(max(0.0, min(100.0, score)), 2)


def _turnover_proxy(volume: float) -> float:
    # The Sequoia cache does not contain free-float shares.  Keep a bounded
    # liquidity proxy so AlphaSift YAML turnover filters remain usable.
    return round(min(max(volume / 1_000_000, 0.0), 30.0), 3)


def _run_strategy(strategy: AlphaSiftStrategy, features: pd.DataFrame) -> List[Dict[str, Any]]:
    df = _apply_filters(features, strategy.hard_filters)
    if df.empty:
        return []
    scored = _score_features(df, strategy)
    output_n = max(strategy.max_output, 1)
    selected = scored.sort_values("screen_score", ascending=False).head(output_n)
    return [_candidate_payload(row, strategy) for _, row in selected.iterrows()]


def _apply_filters(df: pd.DataFrame, filters: Dict[str, Any]) -> pd.DataFrame:
    result = df.copy()
    if result.empty:
        return result
    if filters.get("exclude_st", True):
        result = result[~result["name"].astype(str).str.contains(r"ST|退", na=False)]
    mapping = {
        "price_min": ("price", "min"),
        "price_max": ("price", "max"),
        "amount_min": ("amount", "min"),
        "turnover_rate_min": ("turnover_rate", "min"),
        "volume_ratio_min": ("volume_ratio", "min"),
        "change_pct_min": ("change_pct", "min"),
        "change_pct_max": ("change_pct", "max"),
        "change_60d_min": ("change_60d", "min"),
        "change_60d_max": ("change_60d", "max"),
        "signal_score_min": ("signal_score", "min"),
        "breakout_20d_pct_min": ("breakout_20d_pct", "min"),
        "breakout_20d_pct_max": ("breakout_20d_pct", "max"),
        "range_20d_pct_max": ("range_20d_pct", "max"),
        "volume_ratio_20d_min": ("volume_ratio_20d", "min"),
        "volume_ratio_20d_max": ("volume_ratio_20d", "max"),
        "body_pct_min": ("body_pct", "min"),
        "body_pct_max": ("body_pct", "max"),
        "pullback_to_ma20_pct_min": ("pullback_to_ma20_pct", "min"),
        "pullback_to_ma20_pct_max": ("pullback_to_ma20_pct", "max"),
        "consolidation_days_20d_min": ("consolidation_days_20d", "min"),
        "consolidation_days_20d_max": ("consolidation_days_20d", "max"),
    }
    for filter_key, (column, op) in mapping.items():
        if filters.get(filter_key) is None or column not in result.columns:
            continue
        value = float(filters[filter_key])
        series = pd.to_numeric(result[column], errors="coerce")
        result = result[series >= value] if op == "min" else result[series <= value]
    if filters.get("require_ma_bullish"):
        result = result[result["ma_bullish"] == True]  # noqa: E712
    if filters.get("require_price_above_ma20"):
        result = result[result["price_above_ma20"] == True]  # noqa: E712
    if filters.get("macd_status_whitelist"):
        allowed = {str(item) for item in filters.get("macd_status_whitelist") or []}
        result = result[result["macd_status"].astype(str).isin(allowed)]
    if filters.get("rsi_status_whitelist"):
        allowed = {str(item) for item in filters.get("rsi_status_whitelist") or []}
        result = result[result["rsi_status"].astype(str).isin(allowed)]
    return result


def _score_features(df: pd.DataFrame, strategy: AlphaSiftStrategy) -> pd.DataFrame:
    result = df.copy()
    factor_scores = {
        "momentum": _momentum_score(result),
        "activity": _activity_score(result),
        "stability": _stability_score(result),
        "reversal": _reversal_score(result),
        "liquidity": _liquidity_score(result),
        "value": pd.Series(50.0, index=result.index),
        "size": pd.Series(50.0, index=result.index),
        "theme_heat": pd.Series(50.0, index=result.index),
    }
    weights = {k: float(v) for k, v in (strategy.factor_weights or {}).items() if k in factor_scores}
    if not weights:
        weights = {"momentum": 0.35, "activity": 0.25, "stability": 0.20, "liquidity": 0.20}
    total = sum(max(v, 0.0) for v in weights.values()) or 1.0
    result["screen_score"] = 0.0
    for factor, weight in weights.items():
        result[f"factor_{factor}_score"] = factor_scores[factor].round(4)
        result["screen_score"] += factor_scores[factor] * (max(weight, 0.0) / total)
    result["screen_score"] = result["screen_score"].clip(0, 100)
    return result


def _momentum_score(df: pd.DataFrame) -> pd.Series:
    score = pd.Series(55.0, index=df.index)
    score += pd.to_numeric(df["change_pct"], errors="coerce").fillna(0).clip(-6, 8) * 3.0
    score += pd.to_numeric(df["change_60d"], errors="coerce").fillna(0).clip(-30, 45) * 0.4
    score += (pd.to_numeric(df["signal_score"], errors="coerce").fillna(50) - 50) * 0.25
    return score.clip(0, 100)


def _activity_score(df: pd.DataFrame) -> pd.Series:
    volume_ratio = pd.to_numeric(df["volume_ratio"], errors="coerce").fillna(1.0)
    turnover = pd.to_numeric(df["turnover_rate"], errors="coerce").fillna(0.0)
    score = 55 + (volume_ratio - 1.0).clip(-1, 5) * 9 + turnover.clip(0, 12) * 2.2
    score -= (volume_ratio - 8).clip(lower=0) * 7
    score -= (turnover - 20).clip(lower=0) * 4
    return score.clip(0, 100)


def _stability_score(df: pd.DataFrame) -> pd.Series:
    change_abs = pd.to_numeric(df["change_pct"], errors="coerce").fillna(0).abs()
    volume_ratio = pd.to_numeric(df["volume_ratio"], errors="coerce").fillna(1.0)
    score = 82 - change_abs.clip(0, 10) * 3 - (volume_ratio - 4).clip(lower=0) * 5
    return score.clip(0, 100)


def _reversal_score(df: pd.DataFrame) -> pd.Series:
    change = pd.to_numeric(df["change_pct"], errors="coerce").fillna(0)
    score = 90 - (change + 3.5).abs() * 11
    score -= (-change - 8).clip(lower=0) * 10
    return score.clip(0, 100)


def _liquidity_score(df: pd.DataFrame) -> pd.Series:
    import numpy as np

    amount = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
    return (np.log10(amount.clip(lower=1)) * 12 - 40).clip(0, 100)


def _candidate_payload(row: pd.Series, strategy: AlphaSiftStrategy) -> Dict[str, Any]:
    metrics = {
        key: row.get(key)
        for key in (
            "change_pct",
            "change_60d",
            "volume_ratio",
            "turnover_rate",
            "signal_score",
            "breakout_20d_pct",
            "range_20d_pct",
            "pullback_to_ma20_pct",
            "consolidation_days_20d",
            "macd_status",
            "rsi_value",
            "rsi_status",
            "ma5",
            "ma20",
            "ma60",
            "boll_mid",
            "boll_upper",
            "boll_lower",
            "boll_bandwidth",
            "boll_position",
            "screen_score",
        )
    }
    payload = {
        "code": str(row.get("code")),
        "name": _display_stock_name(row.get("code"), row.get("name")),
        "source": f"alphasift:{strategy.name}",
        "matched_strategies": [strategy.name],
        "strategy_tags": list(strategy.tags),
        "strategy_category": strategy.category,
        "reason": f"{strategy.display_name}：{strategy.description}",
        "signal_score": round(float(row.get("screen_score") or 0), 2),
        "latest_date": str(row.get("date") or ""),
        "price": _jsonable(row.get("price")),
        "change_pct": _jsonable(row.get("change_pct")),
        "amount": _jsonable(row.get("amount")),
        "turnover_rate": _jsonable(row.get("turnover_rate")),
        "ranking_hints": strategy.ranking_hints,
        "metrics": _jsonable_metrics(metrics),
    }
    payload["reason_dimensions"] = _candidate_reason_dimensions(payload)
    return payload


def _merge_candidates(candidates: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_code: Dict[str, Dict[str, Any]] = {}
    for item in candidates:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        if code not in by_code:
            by_code[code] = dict(item)
            continue
        current = by_code[code]
        current["signal_score"] = round(max(float(current.get("signal_score") or 0), float(item.get("signal_score") or 0)) + 3, 2)
        current["matched_strategies"] = _unique([*(current.get("matched_strategies") or []), *(item.get("matched_strategies") or [])])
        current["strategy_tags"] = _unique([*(current.get("strategy_tags") or []), *(item.get("strategy_tags") or [])])
        current["source"] = "alphasift:multi_strategy"
        current["reason"] = f"AlphaSift 多策略共振：{', '.join(current['matched_strategies'])}。"
        metrics = dict(current.get("metrics") or {})
        metrics[str(item.get("source") or "alphasift")] = item.get("metrics") or {}
        current["metrics"] = metrics
    return list(by_code.values())


def _candidate_reason_dimensions(item: Dict[str, Any]) -> List[Dict[str, str]]:
    strategies = _display_strategy_names(item.get("matched_strategies") or [])
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    dimensions: List[Dict[str, str]] = []
    if strategies:
        dimensions.append({
            "dimension": "strategy",
            "label": "策略",
            "detail": f"AlphaSift YAML 多因子策略入池：{'、'.join(strategies)}",
        })
    technical_bits = []
    ma5 = metrics.get("ma5")
    ma20 = metrics.get("ma20")
    ma60 = metrics.get("ma60")
    if ma5 is not None and ma20 is not None:
        ma_text = f"MA5={_short_metric(ma5)}；MA20={_short_metric(ma20)}"
        if ma60 is not None:
            ma_text += f"；MA60={_short_metric(ma60)}"
        technical_bits.append(ma_text)
    macd_status = str(metrics.get("macd_status") or "").strip()
    rsi_status = str(metrics.get("rsi_status") or "").strip()
    rsi_value = metrics.get("rsi_value")
    boll_position = str(metrics.get("boll_position") or "").strip()
    boll_bandwidth = metrics.get("boll_bandwidth")
    if macd_status:
        macd_label = {"bullish": "MACD 多头", "bearish": "MACD 空头", "neutral": "MACD 中性"}.get(macd_status, f"MACD={macd_status}")
        technical_bits.append(macd_label)
    if rsi_value is not None:
        rsi_label = f"RSI={_short_metric(rsi_value)}"
        if rsi_status and rsi_status != "neutral":
            rsi_label += f"（{'超买' if rsi_status == 'overbought' else '超卖'}）"
        technical_bits.append(rsi_label)
    if boll_position:
        boll_label = {
            "above_upper": "布林上轨外运行",
            "upper_half": "布林中上轨运行",
            "lower_half": "布林中下轨运行",
            "below_lower": "布林下轨外运行",
        }.get(boll_position, f"布林位置={boll_position}")
        if boll_bandwidth is not None:
            boll_label += f"；带宽={_short_metric(boll_bandwidth)}%"
        technical_bits.append(boll_label)
    for key, label in (
        ("breakout_20d_pct", "20 日突破幅度"),
        ("range_20d_pct", "20 日区间波动"),
        ("pullback_to_ma20_pct", "回踩 MA20 幅度"),
        ("consolidation_days_20d", "20 日收敛天数"),
    ):
        value = metrics.get(key)
        if value is not None:
            technical_bits.append(f"{label}={value}")
    if technical_bits:
        dimensions.append({"dimension": "technical", "label": "技术面", "detail": "；".join(technical_bits[:6])})
    capital_bits = []
    for key, label in (
        ("amount", "成交额"),
        ("turnover_rate", "换手率"),
        ("volume_ratio", "量比"),
        ("volume_ratio_20d", "20 日量比"),
    ):
        value = item.get(key, metrics.get(key))
        if value is not None:
            capital_bits.append(f"{label}={value}")
    if capital_bits:
        dimensions.append({"dimension": "capital", "label": "资金面", "detail": "流动性代理：" + "；".join(capital_bits[:4])})
    return dimensions


def _display_strategy_names(names: Iterable[Any]) -> List[str]:
    mapping = {
        "volume_breakout": "放量突破",
        "capital_heat": "资金热度",
        "quality_value": "质量价值",
        "shrink_pullback": "缩量回踩",
        "balanced_alpha": "均衡 Alpha",
        "dual_low": "双低价值",
        "momentum_quality": "动量质量",
        "oversold_reversal": "超跌反转",
        "breakout": "突破",
        "momentum": "动量",
        "liquidity": "流动性",
    }
    result: List[str] = []
    for name in names:
        text = mapping.get(str(name), str(name))
        if text and text not in result:
            result.append(text)
    return result


def _short_metric(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return str(value)
    if abs(number) >= 100_000_000:
        return f"{number / 100_000_000:.2f}亿"
    if abs(number) >= 10_000:
        return f"{number / 10_000:.2f}万"
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _unique(items: Iterable[Any]) -> List[str]:
    result: List[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _jsonable(value: Any) -> Any:
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        return round(value, 4)
    return value


def _jsonable_metrics(metrics: Dict[str, Any]) -> Dict[str, Any]:
    return {str(key): _jsonable(value) for key, value in metrics.items() if _jsonable(value) is not None}
