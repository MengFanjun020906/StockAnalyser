# -*- coding: utf-8 -*-
"""Sequoia-style quantitative candidate provider.

This module ports the strategy rules from the local Sequoia-X reference project
into this codebase so candidate discovery does not depend on importing a cloned
sub-repository at runtime.  It reads an existing SQLite daily-bar cache with the
Sequoia-X schema:

    stock_daily(symbol, date, open, high, low, close, volume, turnover)
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd

from src.data.stock_index_loader import get_index_stock_name
from src.data.stock_mapping import STOCK_NAME_MAP, is_meaningful_stock_name

logger = logging.getLogger(__name__)

DEFAULT_SEQUOIA_DB_PATH = "Sequoia-X/data/sequoia_v2.db"

_REQUIRED_COLUMNS = {"symbol", "date", "open", "high", "low", "close", "volume", "turnover"}


@dataclass(frozen=True)
class SequoiaStrategySpec:
    name: str
    min_bars: int
    tags: List[str]
    reason: str


STRATEGY_SPECS: Dict[str, SequoiaStrategySpec] = {
    "ma_volume": SequoiaStrategySpec(
        name="ma_volume",
        min_bars=20,
        tags=["ma_cross", "volume_breakout"],
        reason="5日均线上穿20日均线，且当日成交量超过20日均量1.5倍。",
    ),
    "turtle_trade": SequoiaStrategySpec(
        name="turtle_trade",
        min_bars=21,
        tags=["breakout", "liquidity", "momentum"],
        reason="收盘价突破前20日高点，成交额过亿，并以实体阳线确认。",
    ),
    "high_tight_flag": SequoiaStrategySpec(
        name="high_tight_flag",
        min_bars=40,
        tags=["momentum", "consolidation", "volume_shrink"],
        reason="过去40日强动量上涨后，近10日高位窄幅整理并缩量。",
    ),
    "limit_up_shakeout": SequoiaStrategySpec(
        name="limit_up_shakeout",
        min_bars=3,
        tags=["limit_up", "shakeout", "support_hold"],
        reason="昨日涨停后今日放量收阴，但低点未跌破昨日收盘支撑。",
    ),
    "uptrend_limit_down": SequoiaStrategySpec(
        name="uptrend_limit_down",
        min_bars=60,
        tags=["uptrend", "limit_down", "mean_reversion"],
        reason="20日均线高于60日均线的上升趋势中，出现放量跌停错杀信号。",
    ),
    "rps_breakout": SequoiaStrategySpec(
        name="rps_breakout",
        min_bars=120,
        tags=["rps", "relative_strength", "breakout"],
        reason="120日相对强度位于市场前10%，且收盘价接近120日滚动高点。",
    ),
    "private_placement": SequoiaStrategySpec(
        name="private_placement",
        min_bars=0,
        tags=["private_placement", "announcement", "event"],
        reason="近 7 天存在定向增发公告，属于事件型候选，需要后续核实发行对象、价格和摊薄影响。",
    ),
}


class SequoiaCandidateProvider:
    """Generate A-share seed candidates from a Sequoia-X style SQLite cache."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = str(db_path or _default_db_path())

    def discover(
        self,
        *,
        limit: int = 8,
        strategy_names: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        effective_limit = max(1, min(int(limit or 8), 50))
        strategies = _normalize_strategy_names(strategy_names)
        diagnostics: List[Dict[str, Any]] = []
        event_strategies = [name for name in strategies if name == "private_placement"]
        bar_strategies = [name for name in strategies if name != "private_placement"]
        candidates: List[Dict[str, Any]] = []

        for event_strategy in event_strategies:
            event_candidates, event_diagnostic = _run_event_strategy(event_strategy)
            diagnostics.append(event_diagnostic)
            candidates.extend(event_candidates)

        validation_error = self._validate_db() if bar_strategies else None
        if validation_error and not candidates:
            return {
                "status": "unavailable",
                "provider": "sequoia",
                "db_path": self.db_path,
                "candidates": [],
                "diagnostics": [
                    *diagnostics,
                    {"source": "sequoia_db", "status": "unavailable", "error": validation_error},
                ],
                "error": validation_error,
            }
        if validation_error:
            diagnostics.append({"source": "sequoia_db", "status": "unavailable", "error": validation_error})
            bars = pd.DataFrame()
        elif bar_strategies:
            try:
                bars = self._load_bars()
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                logger.warning("Sequoia candidate DB load failed: %s", message)
                if not candidates:
                    return {
                        "status": "failed",
                        "provider": "sequoia",
                        "db_path": self.db_path,
                        "candidates": [],
                        "diagnostics": [{"source": "stock_daily", "status": "failed", "error": message}],
                        "error": message,
                    }
                diagnostics.append({"source": "stock_daily", "status": "failed", "error": message})
                bars = pd.DataFrame()
        else:
            bars = pd.DataFrame()

        if bars.empty and bar_strategies and not candidates:
            return {
                "status": "empty",
                "provider": "sequoia",
                "db_path": self.db_path,
                "candidates": [],
                "diagnostics": [{"source": "stock_daily", "status": "empty"}],
            }

        latest_date = str(bars["date"].max()) if not bars.empty else None
        for strategy_name in bar_strategies:
            strategy_candidates = self._run_strategy(strategy_name, bars)
            diagnostics.append({
                "source": f"sequoia:{strategy_name}",
                "status": "ok",
                "count": len(strategy_candidates),
            })
            candidates.extend(strategy_candidates)

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
        return {
            "status": "ok" if selected else "empty",
            "provider": "sequoia",
            "db_path": self.db_path,
            "latest_date": latest_date,
            "strategy_names": strategies,
            "candidate_count": len(selected),
            "candidates": selected,
            "diagnostics": diagnostics,
        }

    def _validate_db(self) -> Optional[str]:
        path = Path(self.db_path).expanduser()
        if not path.exists():
            return f"Sequoia candidate DB not found: {path}"
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
        df = df.dropna(subset=["symbol", "date", "close"])
        df = df[df["symbol"].str.fullmatch(r"\d{6}", na=False)]
        df = df[df["volume"].fillna(0) > 0]
        return df.sort_values(["symbol", "date"]).reset_index(drop=True)

    def _run_strategy(self, strategy_name: str, bars: pd.DataFrame) -> List[Dict[str, Any]]:
        if strategy_name == "rps_breakout":
            return _run_rps_breakout(bars)

        spec = STRATEGY_SPECS[strategy_name]
        candidates: List[Dict[str, Any]] = []
        for symbol, df in bars.groupby("symbol", sort=False):
            if len(df) < spec.min_bars:
                continue
            try:
                candidate = _run_single_symbol_strategy(strategy_name, str(symbol), df.copy())
            except Exception as exc:
                logger.debug("Sequoia strategy failed symbol=%s strategy=%s: %s", symbol, strategy_name, exc)
                continue
            if candidate:
                candidates.append(candidate)
        return candidates


def _default_db_path() -> str:
    return os.getenv("SEQUOIA_CANDIDATE_DB_PATH") or DEFAULT_SEQUOIA_DB_PATH


def _normalize_strategy_names(strategy_names: Optional[Sequence[str]]) -> List[str]:
    raw = [str(item).strip().lower() for item in (strategy_names or []) if str(item or "").strip()]
    if not raw or "all" in raw:
        return list(STRATEGY_SPECS.keys())
    aliases = {
        "turtle": "turtle_trade",
        "flag": "high_tight_flag",
        "shakeout": "limit_up_shakeout",
        "limit_down": "uptrend_limit_down",
        "rps": "rps_breakout",
        "placement": "private_placement",
        "private": "private_placement",
        "定增": "private_placement",
    }
    result: List[str] = []
    for name in raw:
        canonical = aliases.get(name, name)
        if canonical in STRATEGY_SPECS and canonical not in result:
            result.append(canonical)
    return result


def _run_single_symbol_strategy(strategy_name: str, symbol: str, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    if strategy_name == "ma_volume":
        df["ma5"] = df["close"].rolling(5).mean()
        df["ma20"] = df["close"].rolling(20).mean()
        df["vol_ma20"] = df["volume"].rolling(20).mean()
        last = df.iloc[-1]
        prev = df.iloc[-2]
        matched = prev["ma5"] < prev["ma20"] and last["ma5"] > last["ma20"] and last["volume"] > last["vol_ma20"] * 1.5
        score = 68 + _bounded_ratio(last["volume"], last["vol_ma20"] * 1.5, cap=20)
        metrics = {"volume_ratio": _safe_ratio(last["volume"], last["vol_ma20"]), "ma5": last["ma5"], "ma20": last["ma20"]}
    elif strategy_name == "turtle_trade":
        df["high_20"] = df["high"].shift(1).rolling(20).max()
        last = df.iloc[-1]
        prev = df.iloc[-2]
        matched = (
            not pd.isna(last["high_20"])
            and last["close"] > last["high_20"]
            and last["turnover"] > 100_000_000
            and last["close"] > last["open"]
            and last["close"] > prev["close"]
        )
        change_pct = _safe_ratio(last["close"] - prev["close"], prev["close"]) * 100
        score = 72 + min(max(change_pct, 0), 20)
        metrics = {"change_pct": change_pct, "high_20": last["high_20"], "turnover": last["turnover"]}
    elif strategy_name == "high_tight_flag":
        tail40 = df.tail(40)
        tail10 = df.tail(10)
        high40 = tail40["high"].max()
        low40 = tail40["low"].min()
        high10 = tail10["high"].max()
        low10 = tail10["low"].min()
        vol_ma20 = df["volume"].iloc[-21:-1].mean()
        matched = (
            low40 > 0
            and low10 > 0
            and high40 / low40 > 1.6
            and high10 / low10 < 1.15
            and low10 >= high40 * 0.8
            and df["volume"].iloc[-1] < vol_ma20 * 0.6
        )
        score = 76 + min(max((high40 / low40 - 1.6) * 30, 0), 18)
        metrics = {"momentum_40": _safe_ratio(high40, low40), "range_10": _safe_ratio(high10, low10), "volume_ratio": _safe_ratio(df["volume"].iloc[-1], vol_ma20)}
    elif strategy_name == "limit_up_shakeout":
        prev2 = df.iloc[-3]
        prev1 = df.iloc[-2]
        today = df.iloc[-1]
        matched = (
            prev1["close"] >= prev2["close"] * 1.095
            and today["close"] < today["open"]
            and today["volume"] > prev1["volume"] * 2.0
            and today["low"] >= prev1["close"]
        )
        score = 74 + _bounded_ratio(today["volume"], prev1["volume"] * 2.0, cap=18)
        metrics = {"prev_limit_up_pct": _safe_ratio(prev1["close"] - prev2["close"], prev2["close"]) * 100, "volume_ratio": _safe_ratio(today["volume"], prev1["volume"])}
    elif strategy_name == "uptrend_limit_down":
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma60"] = df["close"].rolling(60).mean()
        df["vol_ma20"] = df["volume"].rolling(20).mean()
        prev = df.iloc[-2]
        today = df.iloc[-1]
        matched = (
            not pd.isna(prev["ma20"])
            and not pd.isna(prev["ma60"])
            and not pd.isna(today["vol_ma20"])
            and prev["ma20"] > prev["ma60"]
            and today["close"] <= prev["close"] * 0.905
            and today["volume"] > today["vol_ma20"] * 2.0
        )
        score = 66 + _bounded_ratio(today["volume"], today["vol_ma20"] * 2.0, cap=16)
        metrics = {"ma20": prev["ma20"], "ma60": prev["ma60"], "volume_ratio": _safe_ratio(today["volume"], today["vol_ma20"])}
    else:
        return None

    if not matched:
        return None
    common_metrics = _common_technical_metrics(df)
    return _candidate_payload(symbol, strategy_name, df.iloc[-1], score, {**common_metrics, **metrics})


def _run_rps_breakout(bars: pd.DataFrame) -> List[Dict[str, Any]]:
    period = 120
    if bars.empty:
        return []
    df = bars[["symbol", "date", "close", "high"]].copy()
    df["close_shift"] = df.groupby("symbol")["close"].shift(period)
    df["pct_change"] = (df["close"] - df["close_shift"]) / df["close_shift"]
    latest_date = df["date"].max()
    latest_df = df[df["date"] == latest_date].dropna(subset=["pct_change"]).copy()
    if latest_df.empty:
        return []
    latest_df["rps"] = latest_df["pct_change"].rank(pct=True) * 100

    roll_high = (
        df.groupby("symbol")["high"]
        .rolling(window=period, min_periods=period // 2)
        .max()
        .reset_index(level=0, drop=True)
    )
    df["roll_high"] = roll_high
    latest_roll_high = df[df["date"] == latest_date][["symbol", "roll_high"]]
    latest_df = latest_df.merge(latest_roll_high, on="symbol")
    selected = latest_df[(latest_df["rps"] >= 90) & (latest_df["close"] >= latest_df["roll_high"] * 0.90)]

    candidates: List[Dict[str, Any]] = []
    for _, row in selected.iterrows():
        score = min(99.0, 76 + float(row["rps"] - 90) * 1.5)
        symbol = str(row["symbol"])
        symbol_df = bars[bars["symbol"] == symbol].copy()
        common_metrics = _common_technical_metrics(symbol_df)
        metrics = {"rps": row["rps"], "pct_change_120": row["pct_change"] * 100, "roll_high_120": row["roll_high"]}
        candidates.append(_candidate_payload(symbol, "rps_breakout", row, score, {**common_metrics, **metrics}))
    return candidates


def _run_event_strategy(strategy_name: str) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if strategy_name != "private_placement":
        return [], {"source": f"sequoia:{strategy_name}", "status": "unsupported"}
    try:
        import akshare as ak

        df = ak.stock_qbzf_em()
    except Exception as exc:
        return [], {
            "source": "sequoia:private_placement",
            "status": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
        }
    candidates = _private_placement_candidates(df)
    return candidates, {
        "source": "sequoia:private_placement",
        "status": "ok" if candidates else "empty",
        "count": len(candidates),
    }


def _private_placement_candidates(df: Any, *, today: Optional[date] = None) -> List[Dict[str, Any]]:
    if df is None or getattr(df, "empty", True):
        return []
    work = pd.DataFrame(df).copy()
    if "发行方式" in work.columns:
        work = work[work["发行方式"].astype(str) == "定向增发"]
    if work.empty or "发行日期" not in work.columns or "股票代码" not in work.columns:
        return []
    current_date = today or date.today()
    cutoff = current_date - timedelta(days=7)
    work["发行日期"] = pd.to_datetime(work["发行日期"], errors="coerce")
    work = work.dropna(subset=["发行日期"])
    work = work[work["发行日期"].dt.date >= cutoff]
    if work.empty:
        return []
    work = work.sort_values("发行日期", ascending=False)
    by_code: Dict[str, Dict[str, Any]] = {}
    for _, row in work.iterrows():
        code_match = pd.Series([row.get("股票代码")]).astype(str).str.extract(r"(\d{6})").iloc[0, 0]
        code = str(code_match or "").strip()
        if not code or code in by_code:
            continue
        latest_date = _format_date(row.get("发行日期"))
        amount = _jsonable_metrics({"amount": row.get("实际募集资金总额"), "issue_price": row.get("增发价格")})
        metrics = {
            **amount,
            "issue_method": str(row.get("发行方式") or "定向增发"),
            "announcement_date": latest_date,
        }
        by_code[code] = _candidate_payload(
            code,
            "private_placement",
            row,
            70.0,
            metrics,
        )
        by_code[code]["latest_date"] = latest_date
    return list(by_code.values())


def _common_technical_metrics(df: pd.DataFrame) -> Dict[str, Any]:
    if df.empty:
        return {}
    work = df.copy()
    work["ma5"] = work["close"].rolling(5).mean()
    work["ma20"] = work["close"].rolling(20).mean()
    work["ma60"] = work["close"].rolling(60).mean()
    work["vol_ma20"] = work["volume"].rolling(20).mean()
    last = work.iloc[-1]
    macd_status = _macd_status(work["close"])
    rsi_value = _rsi(work["close"], 12)
    rsi_status = "oversold" if rsi_value < 30 else "overbought" if rsi_value > 70 else "neutral"
    boll_mid, boll_upper, boll_lower, boll_bandwidth, boll_position = _bollinger_metrics(work["close"])
    return _jsonable_metrics({
        "ma5": last.get("ma5"),
        "ma20": last.get("ma20"),
        "ma60": last.get("ma60"),
        "volume_ratio": _safe_ratio(last.get("volume"), last.get("vol_ma20")),
        "macd_status": macd_status,
        "rsi_value": rsi_value,
        "rsi_status": rsi_status,
        "boll_mid": boll_mid,
        "boll_upper": boll_upper,
        "boll_lower": boll_lower,
        "boll_bandwidth": boll_bandwidth,
        "boll_position": boll_position,
    })


def _candidate_payload(
    symbol: str,
    strategy_name: str,
    last: Any,
    score: float,
    metrics: Dict[str, Any],
) -> Dict[str, Any]:
    spec = STRATEGY_SPECS[strategy_name]
    payload = {
        "code": symbol,
        "name": _display_stock_name(symbol),
        "source": f"sequoia:{strategy_name}",
        "matched_strategies": [strategy_name],
        "strategy_tags": list(spec.tags),
        "reason": spec.reason,
        "signal_score": round(float(score), 2),
        "latest_date": _format_date(last.get("date") if hasattr(last, "get") else None),
        "metrics": _jsonable_metrics(metrics),
    }
    payload["reason_dimensions"] = _candidate_reason_dimensions(payload)
    return payload


def _display_stock_name(code: Any, current_name: Any = None) -> str:
    code_text = str(code or "").strip()
    current_text = str(current_name or "").strip()
    if is_meaningful_stock_name(current_text, code_text):
        return current_text
    for name in (STOCK_NAME_MAP.get(code_text), get_index_stock_name(code_text)):
        if is_meaningful_stock_name(name, code_text):
            return str(name)
    return code_text


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
        current["signal_score"] = round(max(float(current.get("signal_score") or 0), float(item.get("signal_score") or 0)) + 4, 2)
        current["matched_strategies"] = _unique([*(current.get("matched_strategies") or []), *(item.get("matched_strategies") or [])])
        current["strategy_tags"] = _unique([*(current.get("strategy_tags") or []), *(item.get("strategy_tags") or [])])
        current["source"] = "sequoia:multi_strategy"
        current["reason"] = f"多策略共振：{', '.join(current['matched_strategies'])}。"
        metrics = dict(current.get("metrics") or {})
        metrics[str(item.get("source") or "sequoia")] = item.get("metrics") or {}
        current["metrics"] = metrics
    merged = list(by_code.values())
    for item in merged:
        item["reason_dimensions"] = _candidate_reason_dimensions(item)
    return merged


def _candidate_reason_dimensions(item: Dict[str, Any]) -> List[Dict[str, str]]:
    strategies = _display_strategy_names(item.get("matched_strategies") or [])
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    dimensions: List[Dict[str, str]] = []
    if strategies:
        dimensions.append({
            "dimension": "strategy",
            "label": "策略",
            "detail": f"Sequoia 形态/动量策略入池：{'、'.join(strategies)}",
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
        technical_bits.append({"bullish": "MACD 多头", "bearish": "MACD 空头", "neutral": "MACD 中性"}.get(macd_status, f"MACD={macd_status}"))
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
        ("high_20", "20 日高点"),
        ("volume_ratio", "量比"),
        ("rps", "RPS"),
        ("pct_change_120", "120 日涨幅"),
        ("momentum_40", "40 日动量"),
        ("range_10", "10 日区间"),
    ):
        value = metrics.get(key)
        if value is not None:
            technical_bits.append(f"{label}={value}")
    if technical_bits:
        dimensions.append({"dimension": "technical", "label": "技术面", "detail": "；".join(technical_bits[:6])})
    capital_bits = []
    for key, label in (("turnover", "成交额"), ("volume_ratio", "量比")):
        value = metrics.get(key)
        if value is not None:
            capital_bits.append(f"{label}={value}")
    if capital_bits:
        dimensions.append({"dimension": "capital", "label": "资金面", "detail": "流动性代理：" + "；".join(capital_bits[:3])})
    return dimensions


def _display_strategy_names(names: Iterable[Any]) -> List[str]:
    mapping = {
        "ma_volume": "均线放量突破",
        "turtle_trade": "海龟突破",
        "high_tight_flag": "高窄旗形",
        "limit_up_shakeout": "涨停洗盘",
        "uptrend_limit_down": "上升趋势跌停错杀",
        "rps_breakout": "RPS 强势突破",
        "private_placement": "定增公告事件",
        "breakout": "突破",
        "rps": "RPS 强势",
        "relative_strength": "相对强势",
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


def _safe_ratio(numerator: Any, denominator: Any) -> float:
    try:
        denominator_f = float(denominator)
        if denominator_f == 0:
            return 0.0
        return float(numerator) / denominator_f
    except Exception:
        return 0.0


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


def _bounded_ratio(numerator: Any, denominator: Any, *, cap: float) -> float:
    return min(max((_safe_ratio(numerator, denominator) - 1.0) * 10.0, 0.0), cap)


def _unique(items: Iterable[Any]) -> List[str]:
    result: List[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _jsonable_metrics(metrics: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key, value in metrics.items():
        if isinstance(value, dict):
            payload[str(key)] = _jsonable_metrics(value)
            continue
        try:
            if pd.isna(value):
                continue
        except Exception:
            pass
        if hasattr(value, "item"):
            value = value.item()
        if isinstance(value, float):
            payload[str(key)] = round(value, 4)
        else:
            payload[str(key)] = value
    return payload


def _format_date(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return pd.to_datetime(value).strftime("%Y-%m-%d")
    except Exception:
        return str(value)
