# -*- coding: utf-8 -*-
"""
Market tools — wraps DataFetcherManager market-level methods as agent tools.

Tools:
- get_market_indices: major market index data
- get_sector_rankings: sector performance rankings
- discover_watchlist_candidates: seed stock candidates for stock selection
"""

import logging
import math
import re
import json
import concurrent.futures
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

from src.agent.candidate_experts import CandidateExpertOrchestrator, apply_hard_exclusion
from src.agent.candidate_providers.alphasift_provider import AlphaSiftCandidateProvider
from src.agent.candidate_providers.sequoia_provider import SequoiaCandidateProvider
from src.agent.regime import SentimentComponents, coerce_bars, detect_market_regime
from src.agent.regime_probability import (
    build_path_profile,
    build_regime_probability,
    build_reentry_reference,
    build_sample_quality_summary,
    compute_regime_return_frame,
    compute_symbol_regime_return_frame,
    format_regime_probability_brief,
)
from src.agent.sentiment.news_events import score_news_items
from src.agent.tools.registry import ToolParameter, ToolDefinition
from src.data.stock_index_loader import get_index_stock_name, get_stock_name_index_map
from src.data.stock_mapping import STOCK_NAME_MAP, is_meaningful_stock_name

logger = logging.getLogger(__name__)

_SECTOR_CONSTITUENT_FETCH_TIMEOUT_S = 3.0
_EVENT_IMPACT_MAX_CONFIRMED_THEMES = 4
_MARKET_HISTORY_RUN_CACHE_TTL_SECONDS = 900.0
_MARKET_HISTORY_RUN_CACHE_MAX_ITEMS = 16
_MARKET_HISTORY_RUN_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]], str]] = {}
_MARKET_HISTORY_RUN_CACHE_LOCK = Lock()


def _run_with_timeout(
    task: Callable[[], Any],
    timeout_seconds: float,
    task_name: str,
) -> Tuple[Any, Optional[str], int]:
    start = time.time()
    timeout_value = max(0.0, float(timeout_seconds or 0.0))
    if timeout_value <= 0:
        return None, f"{task_name} timeout", 0

    result_holder: Dict[str, Any] = {}
    error_holder: Dict[str, Exception] = {}

    def runner() -> None:
        try:
            result_holder["value"] = task()
        except Exception as exc:
            error_holder["value"] = exc

    worker = Thread(target=runner, daemon=True, name=f"agent-market-{task_name}")
    worker.start()
    worker.join(timeout=timeout_value)
    if worker.is_alive():
        return None, f"{task_name} timeout", int(timeout_value * 1000)
    duration_ms = int((time.time() - start) * 1000)
    if "value" in error_holder:
        return None, str(error_holder["value"]), duration_ms
    return result_holder.get("value"), None, duration_ms


def _get_agent_timeout_attr(attr_name: str, default: float) -> float:
    try:
        from src.config import get_config

        return float(getattr(get_config(), attr_name, default))
    except Exception:
        return float(default)


def _get_sector_rankings_agent_probe(manager: Any, top_n: int, timeout: float) -> tuple:
    """Probe sector rankings with Agent-safe ordering and per-source budgets."""
    if not hasattr(manager, "_get_fetchers_snapshot") or not hasattr(manager, "_run_with_timeout"):
        result = manager.get_sector_rankings(n=top_n)
        if isinstance(result, tuple) and len(result) == 2:
            return result[0], result[1], [], ""
        return [], [], [], "No sector ranking data available"

    fetcher_order = {
        "TushareFetcher": 0,
        "AkshareFetcher": 1,
        "EfinanceFetcher": 2,
    }
    capable_names = {"AkshareFetcher", "TushareFetcher", "EfinanceFetcher"}
    fetchers = sorted(
        [
            fetcher
            for fetcher in manager._get_fetchers_snapshot()
            if getattr(fetcher, "name", "") in capable_names and hasattr(fetcher, "get_sector_rankings")
        ],
        key=lambda item: (fetcher_order.get(getattr(item, "name", ""), 99), getattr(item, "priority", 99)),
    )
    source_chain: List[Dict[str, Any]] = []
    last_error = ""
    started_at = time.time()
    per_fetcher_timeout = max(0.5, min(float(timeout), 2.5)) if timeout > 0 else 0.0

    for fetcher in fetchers:
        if not hasattr(fetcher, "get_sector_rankings"):
            continue
        remaining_timeout = max(0.0, float(timeout) - (time.time() - started_at))
        if remaining_timeout <= 0:
            last_error = "sector_rankings timeout"
            source_chain.append({
                "provider": "sector_rankings",
                "result": "timeout",
                "duration_ms": int(float(timeout) * 1000),
                "error": last_error,
            })
            break
        data, call_err, duration_ms = manager._run_with_timeout(
            lambda fetcher=fetcher: fetcher.get_sector_rankings(top_n),
            min(remaining_timeout, per_fetcher_timeout),
            f"{getattr(fetcher, 'name', 'fetcher')}_sector_rankings",
        )
        if call_err:
            last_error = f"{getattr(fetcher, 'name', 'fetcher')} {call_err}"
            source_chain.append({
                "provider": getattr(fetcher, "name", "fetcher"),
                "result": "timeout" if "timeout" in str(call_err).lower() else "failed",
                "duration_ms": duration_ms,
                "error": call_err,
            })
            continue
        if isinstance(data, tuple) and len(data) == 2 and data[0] is not None and data[1] is not None:
            source_chain.append({
                "provider": getattr(fetcher, "name", "fetcher"),
                "result": "ok",
                "duration_ms": duration_ms,
            })
            return data[0], data[1], source_chain, ""
        last_error = f"{getattr(fetcher, 'name', 'fetcher')}返回空结果"
        source_chain.append({
            "provider": getattr(fetcher, "name", "fetcher"),
            "result": "empty",
            "duration_ms": duration_ms,
            "error": last_error,
        })

    return [], [], source_chain, last_error


def _get_tushare_sector_rankings_fast(top_n: int, timeout: float) -> Optional[tuple]:
    """Fast-path sector rankings via Tushare THS hot industry-board rankings."""
    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=Path(__file__).resolve().parents[3] / ".env", override=False)
    except Exception:
        pass
    try:
        from data_provider.tushare_client import build_tushare_http_client, get_tushare_token
    except Exception:
        return None
    if not get_tushare_token():
        return None

    started_at = time.time()
    source_chain: List[Dict[str, Any]] = []
    errors: List[str] = []
    client = build_tushare_http_client(timeout=max(1, int(timeout)))

    def remaining(default: float = 4.0) -> float:
        return max(1.0, min(default, float(timeout) - (time.time() - started_at)))

    def _to_float(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except Exception:
            return None
        if math.isnan(number) or math.isinf(number):
            return None
        return number

    def _to_int(value: Any) -> Optional[int]:
        number = _to_float(value)
        if number is None:
            return None
        return int(number)

    def normalize_rows(rows: List[Dict[str, Any]], trade_date: str) -> tuple:
        latest_trade_date = trade_date
        if not latest_trade_date:
            trade_dates = sorted({str(row.get("trade_date") or "").strip() for row in rows if row.get("trade_date")})
            latest_trade_date = trade_dates[-1] if trade_dates else ""
        ranked: List[Dict[str, Any]] = []
        for row in rows:
            row_trade_date = str(row.get("trade_date") or trade_date).strip()
            if latest_trade_date and row_trade_date and row_trade_date != latest_trade_date:
                continue
            data_type = str(row.get("data_type") or "").strip()
            if data_type and data_type != "行业板块":
                continue
            name = str(row.get("ts_name") or row.get("name") or "").strip()
            code = str(row.get("ts_code") or "").strip()
            if not name and not code:
                continue
            rank = _to_int(row.get("rank"))
            item: Dict[str, Any] = {
                "name": name or code,
                "code": code,
                "rank": rank,
                "change_pct": _to_float(row.get("pct_change")),
                "current_price": _to_float(row.get("current_price")),
                "hot": _to_float(row.get("hot")),
                "trade_date": row_trade_date or latest_trade_date,
                "data_type": data_type or "行业板块",
                "rank_time": str(row.get("rank_time") or "").strip(),
                "rank_reason": str(row.get("rank_reason") or "").strip(),
                "source": "tushare:ths_hot",
                "ranking_basis": "THS热榜:market=行业板块",
            }
            ranked.append(item)
        ranked.sort(key=lambda item: item["rank"] if item.get("rank") is not None else 10**9)
        top = ranked[:top_n]
        bottom_candidates = [item for item in ranked if item.get("change_pct") is not None]
        bottom_candidates.sort(key=lambda item: item["change_pct"])
        bottom = bottom_candidates[:top_n]
        return top, bottom

    def _previous_weekday(day):
        day = day - timedelta(days=1)
        while day.weekday() >= 5:
            day = day - timedelta(days=1)
        return day

    now = datetime.now()
    candidate_days = []
    today = now.date()
    if today.weekday() < 5 and now.time() >= datetime.strptime("22:30", "%H:%M").time():
        candidate_days.append(today)
        day = today
    else:
        day = _previous_weekday(today)
        candidate_days.append(day)
    while len(candidate_days) < 5:
        day = _previous_weekday(day)
        candidate_days.append(day)
    candidate_dates = [day.strftime("%Y%m%d") for day in candidate_days]

    for trade_date in candidate_dates:
        if time.time() - started_at >= timeout:
            break
        for is_new in ("Y", "N"):
            call_start = time.time()
            try:
                params = {"market": "行业板块", "is_new": is_new}
                if trade_date:
                    params["trade_date"] = trade_date
                client._timeout = remaining(4.0)
                df = client.query(
                    "ths_hot",
                    **params,
                    fields=(
                        "trade_date,data_type,ts_code,ts_name,rank,pct_change,"
                        "current_price,hot,concept,rank_reason,rank_time"
                    ),
                )
            except Exception as exc:
                errors.append(f"tushare:ths_hot:{exc}")
                source_chain.append({
                    "provider": "tushare:ths_hot",
                    "result": "failed",
                    "duration_ms": int((time.time() - call_start) * 1000),
                    "trade_date": trade_date,
                    "market": "行业板块",
                    "is_new": is_new,
                    "error": str(exc),
                })
                continue
            if df is None or df.empty:
                source_chain.append({
                    "provider": "tushare:ths_hot",
                    "result": "empty",
                    "duration_ms": int((time.time() - call_start) * 1000),
                    "trade_date": trade_date,
                    "market": "行业板块",
                    "is_new": is_new,
                })
                continue
            top, bottom = normalize_rows(df.to_dict(orient="records"), trade_date)
            if top or bottom:
                source_chain.append({
                    "provider": "tushare:ths_hot",
                    "result": "ok",
                    "duration_ms": int((time.time() - call_start) * 1000),
                    "trade_date": trade_date,
                    "market": "行业板块",
                    "is_new": is_new,
                    "ranking_basis": "THS热榜:market=行业板块",
                })
                return top, bottom, source_chain, ""

    if source_chain or errors:
        return [], [], source_chain, " | ".join(errors) or "Tushare ths_hot industry rankings empty"
    return None


def _get_stockapi_sector_rankings_fast(top_n: int) -> tuple:
    """Fast-path sector rankings via StockAPI hot sectors without AkShare fallback."""
    try:
        from data_provider.fundamental_adapter import AkshareFundamentalAdapter
    except Exception as exc:
        return [], [], [], f"stockapi_hot_sectors_import_failed:{exc}"

    result = AkshareFundamentalAdapter().get_stockapi_hot_sectors(
        limit=max(top_n, 20),
        allow_fallback=False,
    )
    sectors = result.get("sectors") or []
    source_chain = result.get("source_chain") or []
    errors = list(result.get("errors") or [])
    if not sectors:
        return [], [], source_chain, " | ".join(errors) or "StockAPI hot sectors empty"

    top: List[Dict[str, Any]] = []
    for sector in sectors[:top_n]:
        name = str(sector.get("bk_name") or sector.get("name") or "").strip()
        if not name:
            continue
        top.append({
            "name": name,
            "change_pct": sector.get("return_pct"),
            "net_inflow": sector.get("net_inflow"),
            "strength": sector.get("strength"),
            "bk_code": sector.get("bk_code"),
            "inflow_days": sector.get("inflow_days"),
            "source": "stockapi:hotBkJlrDr",
        })
    if top:
        return top, [], source_chain or ["stockapi:hotBkJlrDr"], ""
    return [], [], source_chain or ["stockapi:hotBkJlrDr"], "StockAPI hot sectors rows missing sector names"


def _get_eastmoney_industry_rankings_fast(top_n: int) -> tuple:
    """Fast-path sector rankings from Eastmoney's industry-board quote page API."""
    hosts = (
        "https://push2.eastmoney.com/api/qt/clist/get",
        "https://17.push2.eastmoney.com/api/qt/clist/get",
        "https://16.push2.eastmoney.com/api/qt/clist/get",
    )
    params = {
        "pn": "1",
        "pz": "100",
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": "m:90 t:2 f:!50",
        "fields": "f12,f14,f2,f3,f4,f8,f20,f104,f105,f128,f136",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://quote.eastmoney.com/center/boardlist.html#industry_board",
    }
    source_chain: List[Dict[str, Any]] = []
    errors: List[str] = []

    def to_float(value: Any) -> Optional[float]:
        try:
            if value in (None, "", "-"):
                return None
            return float(value)
        except Exception:
            return None

    def to_int(value: Any) -> Optional[int]:
        try:
            if value in (None, "", "-"):
                return None
            return int(float(value))
        except Exception:
            return None

    for url in hosts:
        started_at = time.time()
        try:
            response = requests.get(url, params=params, headers=headers, timeout=2.0)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            error = f"eastmoney:stock_board_industry_name_em:{type(exc).__name__}:{exc}"
            errors.append(error)
            source_chain.append({
                "provider": "eastmoney:stock_board_industry_name_em",
                "endpoint": url,
                "result": "failed",
                "duration_ms": int((time.time() - started_at) * 1000),
                "error": error,
            })
            continue

        rows = ((payload or {}).get("data") or {}).get("diff") or []
        sectors: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("f14") or "").strip()
            if not name:
                continue
            sectors.append({
                "name": name,
                "bk_code": str(row.get("f12") or "").strip(),
                "latest_price": to_float(row.get("f2")),
                "change_pct": to_float(row.get("f3")),
                "change_amount": to_float(row.get("f4")),
                "turnover_rate": to_float(row.get("f8")),
                "market_cap": to_float(row.get("f20")),
                "rising_count": to_int(row.get("f104")),
                "falling_count": to_int(row.get("f105")),
                "leading_stock": str(row.get("f128") or "").strip(),
                "leading_stock_change_pct": to_float(row.get("f136")),
                "source": "eastmoney:stock_board_industry_name_em",
            })
        if sectors:
            sectors.sort(
                key=lambda item: item["change_pct"] if item.get("change_pct") is not None else -9999.0,
                reverse=True,
            )
            top = sectors[:top_n]
            bottom = list(reversed(sectors[-top_n:]))
            return top, bottom, [{
                "provider": "eastmoney:stock_board_industry_name_em",
                "endpoint": url,
                "result": "ok",
                "duration_ms": int((time.time() - started_at) * 1000),
                "rows": len(sectors),
            }], ""

        error = "eastmoney:stock_board_industry_name_em:empty_data"
        errors.append(error)
        source_chain.append({
            "provider": "eastmoney:stock_board_industry_name_em",
            "endpoint": url,
            "result": "empty",
            "duration_ms": int((time.time() - started_at) * 1000),
            "error": error,
        })

    return [], [], source_chain, " | ".join(errors) or "Eastmoney industry board empty"


DEFAULT_WATCHLIST_SEEDS: List[Dict[str, Any]] = [
    {"code": "600519", "name": "贵州茅台", "reason": "大消费核心蓝筹，适合作为稳健配置参照。"},
    {"code": "300750", "name": "宁德时代", "reason": "新能源产业链龙头，适合观察成长主线弹性。"},
    {"code": "688981", "name": "中芯国际", "reason": "半导体制造核心标的，适合承接科技板块强弱判断。"},
    {"code": "002475", "name": "立讯精密", "reason": "消费电子核心标的，适合作为科技制造候选。"},
    {"code": "601318", "name": "中国平安", "reason": "金融权重股，适合作为低估值防守候选。"},
    {"code": "600036", "name": "招商银行", "reason": "银行龙头，适合作为稳健现金流候选。"},
]


def _get_fetcher_manager():
    """Lazy import to avoid circular deps."""
    from data_provider import DataFetcherManager
    return DataFetcherManager()


def _dedupe_candidates(candidates: Iterable[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    return _merge_and_score_candidates(candidates, limit=limit)


def _resolve_candidate_name(code: str, current_name: Any = None) -> str:
    """Resolve a readable display name without triggering realtime quote calls."""
    code_text = str(code or "").strip()
    current_text = str(current_name or "").strip()
    if is_meaningful_stock_name(current_text, code_text):
        return current_text
    for name in (STOCK_NAME_MAP.get(code_text), get_index_stock_name(code_text)):
        if is_meaningful_stock_name(name, code_text):
            return str(name)
    return code_text


def _candidate_base_score(item: Dict[str, Any]) -> float:
    raw_score = item.get("signal_score")
    try:
        return float(raw_score)
    except Exception:
        pass
    for key in ("change_pct", "涨跌幅"):
        if key in item:
            try:
                return max(45.0, min(75.0, 55.0 + float(item.get(key) or 0) * 2.0))
            except Exception:
                continue
    return 50.0


def _candidate_source_family(item: Dict[str, Any]) -> str:
    sources = [str(src or "").strip() for src in item.get("recall_sources") or [] if str(src or "").strip()]
    source = str(item.get("source") or "").strip()
    if source:
        sources.append(source)
    for prefix, family in (
        ("alphasift:", "alphasift"),
        ("sequoia:", "sequoia"),
        ("akshare:", "sector"),
        ("sector_theme:", "sector"),
        ("capital_flow:", "capital"),
        ("event_impact:", "event_impact"),
        ("news_momentum:", "news_momentum"),
        ("news_sentiment:", "news_sentiment"),
        ("user_seed", "user_seed"),
        ("fallback_seed_pool", "fallback"),
    ):
        if any(src == prefix or src.startswith(prefix) for src in sources):
            return family
    if source.startswith("tushare:") or source.startswith("capital_flow:tushare_"):
        return "capital"
    return source or "unknown"


def _candidate_reason_dimensions(item: Dict[str, Any]) -> List[Dict[str, str]]:
    """Build user-facing reason buckets for why a candidate entered L1."""
    dimensions: List[Dict[str, str]] = []

    def add(dimension: str, label: str, detail: str) -> None:
        text = str(detail or "").strip()
        if not text:
            return
        key = (dimension, text)
        if any((entry.get("dimension"), entry.get("detail")) == key for entry in dimensions):
            return
        dimensions.append({"dimension": dimension, "label": label, "detail": text})

    source = str(item.get("source") or "").strip()
    recall_sources = [str(src).strip() for src in item.get("recall_sources") or [] if str(src or "").strip()]
    sources = recall_sources or ([source] if source else [])
    strategies = [str(value).strip() for value in item.get("matched_strategies") or [] if str(value or "").strip()]
    tags = [str(value).strip() for value in item.get("strategy_tags") or [] if str(value or "").strip()]
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}

    strategy_labels = _display_strategy_names(strategies)
    alphasift_sources = [src for src in sources if src.startswith("alphasift:")]
    sequoia_sources = [src for src in sources if src.startswith("sequoia:")]
    sector_sources = [src for src in sources if src.startswith(("akshare:", "sector_theme:"))]

    if alphasift_sources:
        detail = "AlphaSift YAML 多因子策略入池"
        if strategy_labels:
            detail += f"：{'、'.join(strategy_labels)}"
        add("strategy", "策略", detail)
    elif sequoia_sources:
        detail = "Sequoia 形态/动量策略入池"
        if strategy_labels:
            detail += f"：{'、'.join(strategy_labels)}"
        add("strategy", "策略", detail)
    elif strategy_labels:
        add("strategy", "策略", f"命中策略：{'、'.join(strategy_labels)}")

    for src in sector_sources:
        if src.startswith("sector_theme:"):
            label = {
                "sector_theme:tushare_moneyflow_ind_ths": "TuShare THS行业资金流",
                "sector_theme:tushare_moneyflow_cnt_ths": "TuShare THS概念资金流",
                "sector_theme:tushare_moneyflow_ind_dc": "TuShare 东财板块资金流",
            }.get(src, src.replace("sector_theme:", ""))
            board_name = str(metrics.get("board_name") or "").strip()
            board_flow = metrics.get("board_net_inflow")
            board_change = metrics.get("board_change_ratio")
            detail_parts = [f"来源：{label}"]
            if board_name:
                detail_parts.append(f"主题：{board_name}")
            if board_flow is not None:
                detail_parts.append(f"板块净流入={_short_metric(board_flow)}")
            if board_change is not None:
                detail_parts.append(f"板块涨跌幅={_short_metric(board_change)}")
            add("sentiment", "板块主题", "；".join(detail_parts))
        else:
            sector = src.split(":")[-1] if ":" in src else ""
            add("sentiment", "情绪/热点", f"来自强势板块「{sector}」成分股" if sector else "来自强势板块成分股")

    news_sources = [src for src in sources if src.startswith(("news_sentiment:", "news_momentum:"))]
    if news_sources:
        topic = str(item.get("news_topic") or item.get("hot_topic") or "").strip()
        headline = str(item.get("news_title") or item.get("headline") or "").strip()
        source_name = str(item.get("news_source") or "").strip()
        published = str(item.get("published_date") or "").strip()
        score = item.get("message_score")
        state = str(item.get("message_state") or "").strip()
        detail_parts = []
        if topic:
            detail_parts.append(f"热点主题：{topic}")
        if headline:
            detail_parts.append(f"新闻：{headline}")
        if score is not None:
            detail_parts.append(f"消息评分：{_short_metric(score)}")
        if state:
            detail_parts.append(f"状态：{state}")
        if source_name:
            detail_parts.append(f"来源：{source_name}")
        if published:
            detail_parts.append(f"日期：{published}")
        label = "消息面" if any(src.startswith("news_momentum:") for src in news_sources) else "情绪/热点"
        dimension = "message" if label == "消息面" else "sentiment"
        add(dimension, label, "；".join(detail_parts) or "被近期公司级新闻/公告事件提及")

    event_sources = [src for src in sources if src.startswith("event_impact:")]
    if event_sources:
        event_title = str(item.get("event_title") or "").strip()
        theme = str(item.get("validated_theme") or "").strip()
        validation_title = str(item.get("validation_title") or "").strip()
        detail_parts = []
        if event_title:
            detail_parts.append(f"事件：{event_title}")
        if theme:
            detail_parts.append(f"验证主题：{theme}")
        if validation_title:
            detail_parts.append(f"后续事实：{validation_title}")
        add("sentiment", "情绪/事件", "；".join(detail_parts) or "事件传导验证后的主题成分候选")

    capital_sources = [src for src in sources if src.startswith("capital_flow:")]
    if capital_sources:
        detail_parts = []
        source_labels = {
            "capital_flow:tushare_moneyflow_ths": "THS资金流",
            "capital_flow:tushare_moneyflow_dc": "东财资金流",
            "capital_flow:tushare_dragon_tiger_list": "龙虎榜",
            "capital_flow:tushare_dragon_tiger_inst": "龙虎榜席位",
            "capital_flow:tushare_limit_list_ths": "THS涨停榜",
            "capital_flow:tushare_limit_list_d": "涨停榜",
            "capital_flow:tushare_limit_step": "连板天梯",
            "capital_flow:tushare_hot_rank": "热榜",
            "capital_flow:limit_up_pool": "涨停池",
            "capital_flow:popularity_rank": "人气榜",
            "capital_flow:hot_money_activity": "游资/龙虎榜",
            "capital_flow:multi_source": "多资金来源",
        }
        labels = [source_labels.get(src, src.replace("capital_flow:", "")) for src in capital_sources]
        if labels:
            detail_parts.append("来源：" + "、".join(list(dict.fromkeys(labels))[:3]))
        for key, label in (
            ("limit_up_streak", "连板数"),
            ("ceiling_amount", "封单额"),
            ("turnover_ratio", "换手率"),
            ("popularity", "人气值"),
            ("net_inflow", "净买入"),
        ):
            value = item.get(key, metrics.get(key))
            if value is not None:
                detail_parts.append(f"{label}={_short_metric(value)}")
        add("capital", "资金面", "；".join(detail_parts) or "资金活跃度候选")

    reason = str(item.get("reason") or "").strip()
    if reason and not dimensions:
        add("strategy", "策略", reason)
    elif reason and "多路召回" not in reason and "多策略共振" not in reason:
        add("technical", "技术面", _display_reason_text(reason))

    technical_bits: List[str] = []
    if any(token in tags for token in ("breakout", "volume_breakout", "rps", "relative_strength", "momentum", "ma_cross")):
        technical_bits.append("形态/趋势信号满足候选条件")
    for key, label in (
        ("breakout_20d_pct", "20 日突破幅度"),
        ("range_20d_pct", "20 日区间波动"),
        ("pullback_to_ma20_pct", "回踩 MA20 幅度"),
        ("consolidation_days_20d", "20 日收敛天数"),
        ("rps", "RPS 强度"),
    ):
        value = metrics.get(key)
        if value is not None:
            technical_bits.append(f"{label}={_short_metric(value)}")
    if technical_bits:
        add("technical", "技术面", "；".join(technical_bits[:3]))

    capital_bits: List[str] = []
    for key, label in (
        ("amount", "成交额"),
        ("turnover", "成交额"),
        ("turnover_rate", "换手率"),
        ("volume_ratio", "量比"),
        ("volume_ratio_20d", "20 日量比"),
    ):
        value = item.get(key, metrics.get(key))
        if value is not None:
            capital_bits.append(f"{label}={_short_metric(value)}")
    if capital_bits:
        add("capital", "资金面", "流动性代理：" + "；".join(capital_bits[:4]))

    if source == "user_seed":
        add("message", "消息/输入", "用户或上下文提供，优先进入候选池")
    elif source == "fallback_seed_pool":
        add("strategy", "策略", "固定种子池兜底，仅用于保证后续取证链路可运行")

    return dimensions[:5]


def _display_strategy_names(names: Iterable[str]) -> List[str]:
    mapping = {
        "ma_volume": "均线放量突破",
        "turtle_trade": "海龟突破",
        "high_tight_flag": "高窄旗形",
        "limit_up_shakeout": "涨停洗盘",
        "uptrend_limit_down": "上升趋势跌停错杀",
        "rps_breakout": "RPS 强势突破",
        "volume_breakout": "放量突破",
        "capital_heat": "资金热度",
        "quality_value": "质量价值",
        "shrink_pullback": "缩量回踩",
        "balanced_alpha": "均衡 Alpha",
        "dual_low": "双低价值",
        "momentum_quality": "动量质量",
        "oversold_reversal": "超跌反转",
        "hot_sector": "强势板块",
        "breakout": "突破",
        "rps": "RPS 强势",
        "momentum": "动量",
        "relative_strength": "相对强势",
        "volume_shrink": "缩量",
        "consolidation": "平台整理",
        "liquidity": "流动性",
    }
    result: List[str] = []
    for name in names:
        text = mapping.get(str(name), str(name))
        if text and text not in result:
            result.append(text)
    return result


def _display_reason_text(reason: str) -> str:
    text = reason
    for raw, display in {
        "ma_volume": "均线放量突破",
        "turtle_trade": "海龟突破",
        "rps_breakout": "RPS 强势突破",
        "high_tight_flag": "高窄旗形",
        "limit_up_shakeout": "涨停洗盘",
        "uptrend_limit_down": "上升趋势跌停错杀",
    }.items():
        text = text.replace(raw, display)
    return text


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


def _merge_and_score_candidates(candidates: Iterable[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    by_code: Dict[str, Dict[str, Any]] = {}
    for item in candidates:
        code = str(item.get("code") or item.get("stock_code") or "").strip()
        if not code:
            continue
        source = str(item.get("source") or "unknown").strip() or "unknown"
        base_score = _candidate_base_score(item)
        if code not in by_code:
            payload = dict(item)
            payload["code"] = code
            payload["name"] = _resolve_candidate_name(code, payload.get("name") or payload.get("stock_name"))
            payload["recall_sources"] = [source]
            payload["raw_recall_count"] = 1
            payload["signal_score"] = round(base_score, 2)
            by_code[code] = payload
            continue

        current = by_code[code]
        current["name"] = _resolve_candidate_name(code, current.get("name") or item.get("name") or item.get("stock_name"))
        sources = list(current.get("recall_sources") or [])
        if source not in sources:
            sources.append(source)
        current["recall_sources"] = sources
        current["raw_recall_count"] = int(current.get("raw_recall_count") or 1) + 1
        current["signal_score"] = round(max(float(current.get("signal_score") or 0), base_score) + min(len(sources) - 1, 3) * 4, 2)

        current_strategies = list(current.get("matched_strategies") or [])
        for strategy in item.get("matched_strategies") or []:
            strategy_text = str(strategy or "").strip()
            if strategy_text and strategy_text not in current_strategies:
                current_strategies.append(strategy_text)
        if current_strategies:
            current["matched_strategies"] = current_strategies

        current_tags = list(current.get("strategy_tags") or [])
        for tag in item.get("strategy_tags") or []:
            tag_text = str(tag or "").strip()
            if tag_text and tag_text not in current_tags:
                current_tags.append(tag_text)
        if current_tags:
            current["strategy_tags"] = current_tags

        if len(sources) > 1:
            current["source"] = "multi_recall"
            current["reason"] = f"多路召回共振：{', '.join(sources)}。"

        metrics = dict(current.get("metrics") or {})
        if item.get("metrics"):
            metrics[source] = item.get("metrics")
        for key in ("change_pct", "price", "amount", "turnover_rate", "pe_ratio", "pb_ratio"):
            if key in item and key not in current:
                current[key] = item.get(key)
        if metrics:
            current["metrics"] = metrics

    merged = list(by_code.values())
    for item in merged:
        item["reason_dimensions"] = _candidate_reason_dimensions(item)
    merged.sort(
        key=lambda item: (
            float(item.get("signal_score") or 0),
            len(item.get("recall_sources") or []),
            len(item.get("matched_strategies") or []),
            str(item.get("code") or ""),
        ),
        reverse=True,
    )
    effective_limit = max(1, limit)
    if effective_limit <= 1:
        return merged[:effective_limit]

    selected: List[Dict[str, Any]] = []
    selected_codes: set[str] = set()

    def append(item: Dict[str, Any]) -> None:
        code = str(item.get("code") or "")
        if not code or code in selected_codes or len(selected) >= effective_limit:
            return
        selected.append(item)
        selected_codes.add(code)

    if merged:
        append(merged[0])

    top_by_family: Dict[str, Dict[str, Any]] = {}
    for item in merged:
        family = _candidate_source_family(item)
        if family not in top_by_family:
            top_by_family[family] = item
    for family in ("event_impact", "news_momentum", "news_sentiment", "alphasift", "sequoia", "sector", "user_seed", "fallback"):
        item = top_by_family.get(family)
        if item is not None:
            append(item)

    for item in merged:
        append(item)

    return selected


def _apply_candidate_hard_exclusion_for_response(candidates: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Apply hard-exclusion rules for non expert-graph discovery modes."""
    filtered, diagnostics = apply_hard_exclusion(candidates)
    for item in filtered:
        item.setdefault("lifecycle_status", "new")
    return filtered, diagnostics


def _build_candidate_quality_summary(candidates: List[Dict[str, Any]], hard_exclusion: Dict[str, Any]) -> Dict[str, Any]:
    dimension_counts: Dict[str, int] = {}
    source_counts: Dict[str, int] = {}
    lifecycle_counts: Dict[str, int] = {}
    multi_source_count = 0
    fallback_count = 0
    for item in candidates:
        sources = [str(src) for src in item.get("recall_sources") or [] if str(src)]
        if not sources and item.get("source"):
            sources = [str(item.get("source"))]
        if len(sources) > 1:
            multi_source_count += 1
        family = _candidate_source_family(item)
        source_counts[family] = source_counts.get(family, 0) + 1
        if family == "fallback":
            fallback_count += 1
        lifecycle_status = str(item.get("lifecycle_status") or "new")
        lifecycle_counts[lifecycle_status] = lifecycle_counts.get(lifecycle_status, 0) + 1
        dimensions = item.get("candidate_dimensions") or [
            entry.get("dimension")
            for entry in item.get("reason_dimensions") or []
            if isinstance(entry, dict)
        ]
        for dimension in dimensions or ["unknown"]:
            key = str(dimension or "unknown")
            dimension_counts[key] = dimension_counts.get(key, 0) + 1
    hard_strategy_count = source_counts.get("alphasift", 0) + source_counts.get("sequoia", 0)
    return {
        "candidate_count": len(candidates),
        "source_counts": source_counts,
        "dimension_counts": dimension_counts,
        "lifecycle_counts": lifecycle_counts,
        "multi_source_count": multi_source_count,
        "fallback_count": fallback_count,
        "hard_strategy_trunk_missing": hard_strategy_count == 0,
        "hard_exclusion_count": int(hard_exclusion.get("excluded_count") or 0),
    }


def _normalize_stock_candidate(row: Dict[str, Any], *, source: str, reason: str) -> Optional[Dict[str, Any]]:
    code = row.get("代码") or row.get("股票代码") or row.get("code") or row.get("stock_code")
    name = row.get("名称") or row.get("股票名称") or row.get("name") or row.get("stock_name")
    if not code:
        return None
    payload: Dict[str, Any] = {
        "code": str(code).strip(),
        "name": str(name or code).strip(),
        "source": source,
        "reason": reason,
    }
    for src_key, dst_key in (
        ("涨跌幅", "change_pct"),
        ("最新价", "price"),
        ("成交额", "amount"),
        ("换手率", "turnover_rate"),
        ("市盈率-动态", "pe_ratio"),
        ("市净率", "pb_ratio"),
    ):
        if src_key in row:
            payload[dst_key] = row.get(src_key)
    return payload


def _top_sector_names(top_n: int) -> List[str]:
    result = _handle_get_sector_rankings(top_n=top_n)
    sectors = result.get("top_sectors") or result.get("sectors") or []
    names: List[str] = []
    for item in sectors:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("板块名称") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _discover_local_strategy_candidates_for_sector_fallback(
    *,
    effective_limit: int,
    strategy_names: Optional[List[str]],
    reason: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Use local strategy providers when sector constituent APIs are unavailable."""
    provider_limit = min(50, max(effective_limit * 3, effective_limit))
    candidates: List[Dict[str, Any]] = []
    steps: List[Dict[str, Any]] = []
    providers: Tuple[Tuple[str, Callable[[], Dict[str, Any]]], ...] = (
        (
            "sector_local_fallback:alphasift",
            lambda: AlphaSiftCandidateProvider().discover(limit=provider_limit, strategy_names=strategy_names),
        ),
        (
            "sector_local_fallback:sequoia",
            lambda: SequoiaCandidateProvider().discover(limit=provider_limit, strategy_names=strategy_names),
        ),
    )
    for source, discover in providers:
        try:
            result = discover()
        except Exception as exc:
            steps.append({
                "source": source,
                "status": "failed",
                "count": 0,
                "reason": reason,
                "error": str(exc),
            })
            continue
        provider_candidates = result.get("candidates") or []
        steps.append({
            "source": source,
            "status": result.get("status", "failed"),
            "count": len(provider_candidates),
            "reason": reason,
            "provider": result.get("provider"),
            "db_path": result.get("db_path"),
            "strategy_names": result.get("strategy_names", []),
            "diagnostics": result.get("diagnostics", []),
            **({"error": result.get("error")} if result.get("error") else {}),
        })
        candidates.extend(provider_candidates)
    return candidates, steps


def _fetch_sector_constituents(
    sector_name: str,
    limit: int,
    *,
    include_diagnostics: bool = False,
) -> Union[List[Dict[str, Any]], Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
    diagnostics: List[Dict[str, Any]] = []
    try:
        import akshare as ak
    except Exception as exc:
        logger.warning("AkShare unavailable for candidate discovery: %s", exc)
        diagnostics.append({"source": "akshare", "status": "unavailable", "error": str(exc)})
        return ([], diagnostics) if include_diagnostics else []

    fetchers = (
        ("industry", getattr(ak, "stock_board_industry_cons_em", None)),
        ("concept", getattr(ak, "stock_board_concept_cons_em", None)),
    )
    candidates: List[Dict[str, Any]] = []
    for source, fetcher in fetchers:
        if fetcher is None:
            diagnostics.append({"source": f"akshare:{source}", "status": "unavailable"})
            continue
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        call_start = time.time()
        try:
            future = pool.submit(fetcher, symbol=sector_name)
            try:
                df = future.result(timeout=_SECTOR_CONSTITUENT_FETCH_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                future.cancel()
                diagnostics.append({
                    "source": f"akshare:{source}",
                    "status": "timeout",
                    "sector": sector_name,
                    "timeout_s": _SECTOR_CONSTITUENT_FETCH_TIMEOUT_S,
                    "duration_ms": int((time.time() - call_start) * 1000),
                })
                logger.warning(
                    "Candidate discovery sector constituent fetch timed out for sector=%s source=%s after %.1fs",
                    sector_name,
                    source,
                    _SECTOR_CONSTITUENT_FETCH_TIMEOUT_S,
                )
                continue
        except Exception as exc:
            diagnostics.append({
                "source": f"akshare:{source}",
                "status": "failed",
                "sector": sector_name,
                "duration_ms": int((time.time() - call_start) * 1000),
                "error": str(exc),
            })
            logger.debug("Candidate discovery failed for sector=%s source=%s: %s", sector_name, source, exc)
            continue
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if df is None or getattr(df, "empty", True):
            diagnostics.append({
                "source": f"akshare:{source}",
                "status": "empty",
                "sector": sector_name,
                "duration_ms": int((time.time() - call_start) * 1000),
            })
            continue
        for row in df.head(limit).to_dict(orient="records"):
            normalized = _normalize_stock_candidate(
                row,
                source=f"akshare:{source}:{sector_name}",
                reason=f"来自强势板块「{sector_name}」成分股。",
            )
            if normalized:
                candidates.append(normalized)
        diagnostics.append({
            "source": f"akshare:{source}",
            "status": "ok" if candidates else "empty",
            "sector": sector_name,
            "count": len(candidates),
            "duration_ms": int((time.time() - call_start) * 1000),
        })
        if candidates:
            break
    return (candidates, diagnostics) if include_diagnostics else candidates


def _data_tool_result(tool_name: str, *, limit: int, **kwargs: Any) -> Dict[str, Any]:
    handler_name = f"_handle_{tool_name}"
    if tool_name in {
        "get_tushare_moneyflow_ind_ths",
        "get_tushare_moneyflow_ind_dc",
        "get_tushare_moneyflow_cnt_ths",
        "get_tushare_ths_member",
        "get_tushare_announcements",
        "get_tushare_stock_alerts",
        "get_tushare_stock_shock",
        "get_tushare_pledge_stat",
        "get_tushare_pledge_detail",
        "get_tushare_share_float",
        "get_tushare_holder_trade",
        "get_tushare_repurchase",
        "get_tushare_daily_basic",
        "get_tushare_financial_indicators",
        "get_tushare_forecast",
        "get_tushare_express",
        "get_tushare_dividend",
        "get_tushare_adj_factor",
        "get_tushare_index_daily",
        "get_tushare_trade_calendar",
        "get_tushare_moneyflow_ths",
        "get_tushare_moneyflow_dc",
        "get_tushare_dragon_tiger_list",
        "get_tushare_dragon_tiger_inst",
        "get_tushare_limit_list_ths",
        "get_tushare_limit_list_d",
        "get_tushare_limit_step",
        "get_tushare_hot_rank",
    }:
        timeout = _get_agent_timeout_attr("agent_tushare_tool_timeout_seconds", 5.0)
    else:
        timeout = 2.5 if tool_name != "get_stockapi_hot_money_activity" else 2.0
    try:
        from src.agent.tools import data_tools

        handler = getattr(data_tools, handler_name)
    except Exception as exc:
        return {
            "status": "unavailable",
            "items": [],
            "errors": [f"{handler_name} unavailable: {exc}"],
            "source_chain": [],
        }
    call_kwargs = dict(kwargs)
    if "limit" not in call_kwargs:
        call_kwargs["limit"] = max(1, min(int(limit or 10), 30))
    result, err, duration_ms = _run_with_timeout(
        lambda: handler(**call_kwargs),
        timeout,
        handler_name,
    )
    if err:
        return {
            "status": "timeout" if "timeout" in str(err).lower() else "failed",
            "items": [],
            "errors": [str(err)],
            "source_chain": [{
                "provider": handler_name,
                "result": "timeout" if "timeout" in str(err).lower() else "failed",
                "duration_ms": duration_ms,
            }],
        }
    return result if isinstance(result, dict) else {"status": "failed", "items": [], "errors": [f"{handler_name} returned non-dict"]}


def _stockapi_tool_result(tool_name: str, *, limit: int) -> Dict[str, Any]:
    return _data_tool_result(tool_name, limit=limit)


_NEWS_SENTIMENT_TOPICS = (
    "A股 科技 商业 热点 新闻 人工智能 半导体 机器人 新能源 汽车",
    "中国 科技 公司 最新 新闻 AI 芯片 算力 机器人",
    "全球 科技 商业 热点 对中国上市公司影响 AI 半导体 新能源",
)

_EVENT_IMPACT_QUERIES = (
    "全球 宏观 地缘 政策 商业 科技 突发 新闻 对市场影响",
    "中国 美国 贸易 关税 科技 出口限制 谈判 最新进展",
    "霍尔木兹 原油 航运 保险费 通行 能源 市场 最新进展",
    "AI 半导体 新能源 机器人 产业政策 商业热点 最新进展",
)

_EVENT_IMPACT_RULES: List[Dict[str, Any]] = [
    {
        "event_type": "trade_policy",
        "keywords": ("关税", "贸易谈判", "中美", "出口限制", "制裁", "访华"),
        "variables": ("tariff_expectation", "export_order_visibility", "supply_chain_risk"),
        "themes": ("出口链", "消费电子", "汽车零部件", "跨境电商", "半导体"),
        "validation_terms": ("关税", "订单", "出口", "供应链", "板块异动", "资金流入"),
    },
    {
        "event_type": "geopolitical_energy",
        "keywords": ("霍尔木兹", "原油", "油价", "航运", "通行", "中东", "地缘"),
        "variables": ("oil_risk_premium", "shipping_cost", "risk_appetite"),
        "themes": ("石油石化", "航运港口", "化工", "航空机场"),
        "validation_terms": ("油价", "运价", "保险费", "板块异动", "资金流入"),
    },
    {
        "event_type": "technology_policy",
        "keywords": ("人工智能", "AI", "芯片", "半导体", "算力", "机器人", "出口管制"),
        "variables": ("compute_demand", "semiconductor_policy_risk", "automation_capex"),
        "themes": ("人工智能", "半导体", "算力", "机器人", "工业自动化"),
        "validation_terms": ("订单", "政策", "新品", "算力", "板块异动", "资金流入"),
    },
    {
        "event_type": "green_industry",
        "keywords": ("新能源", "储能", "光伏", "电池", "电动车", "碳中和"),
        "variables": ("green_capex", "battery_demand", "export_order_visibility"),
        "themes": ("新能源车", "储能", "光伏", "锂电池"),
        "validation_terms": ("订单", "装机", "出口", "价格", "板块异动", "资金流入"),
    },
]

_GENERIC_NEWS_NAME_BLOCKLIST = {
    "中国",
    "科技",
    "股份",
    "集团",
    "证券",
    "银行",
    "能源",
    "汽车",
    "智能",
    "半导体",
    "机器人",
    "人工智能",
}


def _build_search_service_for_candidates():
    """Build SearchService from project config for candidate discovery."""
    try:
        from src.config import get_config
        from src.search_service import SearchService
    except Exception as exc:
        logger.debug("News sentiment candidate search unavailable: %s", exc)
        return None

    try:
        config = get_config()
        return SearchService(
            bocha_keys=getattr(config, "bocha_api_keys", []),
            tavily_keys=getattr(config, "tavily_api_keys", []),
            anspire_keys=getattr(config, "anspire_api_keys", []),
            brave_keys=getattr(config, "brave_api_keys", []),
            serpapi_keys=getattr(config, "serpapi_keys", []),
            minimax_keys=getattr(config, "minimax_api_keys", []),
            searxng_base_urls=getattr(config, "searxng_base_urls", []),
            searxng_public_instances_enabled=getattr(config, "searxng_public_instances_enabled", True),
            news_max_age_days=getattr(config, "news_max_age_days", 3),
            news_strategy_profile=getattr(config, "news_strategy_profile", "short"),
        )
    except Exception as exc:
        logger.warning("News sentiment candidate search init failed: %s", exc)
        return None


def _iter_candidate_name_pairs() -> List[tuple[str, str]]:
    """Return A-share code/name pairs for lightweight news entity matching."""
    pairs: Dict[str, str] = {}
    for code, name in STOCK_NAME_MAP.items():
        code_text = str(code or "").strip()
        if code_text.isdigit() and len(code_text) == 6 and is_meaningful_stock_name(name, code_text):
            pairs[code_text] = str(name).strip()

    for code, name in get_stock_name_index_map().items():
        code_text = str(code or "").strip().upper()
        if code_text.endswith((".SH", ".SZ", ".BJ")):
            code_text = code_text.rsplit(".", 1)[0]
        if not (code_text.isdigit() and len(code_text) == 6):
            continue
        if is_meaningful_stock_name(name, code_text):
            pairs.setdefault(code_text, str(name).strip())

    items = [
        (code, name)
        for code, name in pairs.items()
        if len(name) >= 2 and name not in _GENERIC_NEWS_NAME_BLOCKLIST
    ]
    return sorted(items, key=lambda item: len(item[1]), reverse=True)


def _match_a_share_mentions(text: str, *, max_matches: int = 5) -> List[tuple[str, str]]:
    haystack = str(text or "")
    if not haystack:
        return []
    matches: List[tuple[str, str]] = []
    seen: set[str] = set()
    for code, name in _iter_candidate_name_pairs():
        if code in seen:
            continue
        if name and name in haystack:
            matches.append((code, name))
            seen.add(code)
            if len(matches) >= max_matches:
                break
    for code in re.findall(r"(?<!\d)([0368]\d{5})(?!\d)", haystack):
        if code in seen:
            continue
        matches.append((code, _resolve_candidate_name(code)))
        seen.add(code)
        if len(matches) >= max_matches:
            break
    return matches


def _search_news_sentiment_candidates(limit: int) -> Dict[str, Any]:
    service = _build_search_service_for_candidates()
    if service is None or not getattr(service, "is_available", False):
        return {
            "status": "unavailable",
            "candidates": [],
            "queries": [],
            "diagnostics": [{"source": "search_service", "status": "unavailable", "reason": "No search provider configured"}],
        }

    candidates: List[Dict[str, Any]] = []
    queries: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()
    effective_limit = max(1, min(limit, 20))
    for query in _NEWS_SENTIMENT_TOPICS:
        try:
            response = service.search_stock_news(
                stock_code="000001",
                stock_name="A股科技商业热点",
                max_results=5,
                focus_keywords=[query],
            )
        except Exception as exc:
            queries.append({"query": query, "status": "failed", "error": str(exc)})
            continue

        query_step = {
            "query": response.query,
            "status": "ok" if response.success else "failed",
            "provider": response.provider,
            "count": len(response.results),
            **({"error": response.error_message} if response.error_message else {}),
        }
        queries.append(query_step)
        if not response.success:
            continue

        for result in response.results:
            url = str(getattr(result, "url", "") or "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            text = " ".join([
                str(getattr(result, "title", "") or ""),
                str(getattr(result, "snippet", "") or ""),
            ])
            for code, name in _match_a_share_mentions(text):
                candidates.append({
                    "code": code,
                    "name": name,
                    "source": "news_sentiment:hot_news",
                    "reason": "近期科技/商业热点新闻提及。",
                    "signal_score": 68.0,
                    "news_topic": query,
                    "news_title": str(getattr(result, "title", "") or "")[:160],
                    "news_snippet": str(getattr(result, "snippet", "") or "")[:240],
                    "news_url": url,
                    "news_source": str(getattr(result, "source", "") or ""),
                    "published_date": getattr(result, "published_date", None),
                    "strategy_tags": ["hot_news", "sentiment"],
                    "metrics": {"news_hits": 1},
                })
                if len(candidates) >= effective_limit * 2:
                    break
            if len(candidates) >= effective_limit * 2:
                break
        if len(candidates) >= effective_limit:
            break

    merged = _dedupe_candidates(candidates, effective_limit)
    status = "ok" if merged else ("empty" if any(item.get("status") == "ok" for item in queries) else "failed")
    return {
        "status": status,
        "candidates": merged,
        "queries": queries,
        "diagnostics": [],
    }


def _stock_news_items_for_candidate(service: Any, code: str, name: str) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    query = f"{name} {code} 公告 业绩 订单 合作 减持 监管 问询 回购 增持 最近"
    try:
        try:
            response = service.search_stock_news(
                stock_code=code,
                stock_name=name,
                max_results=5,
                focus_keywords=[query],
                max_provider_attempts=2,
            )
        except TypeError as exc:
            if "max_provider_attempts" not in str(exc):
                raise
            response = service.search_stock_news(
                stock_code=code,
                stock_name=name,
                max_results=5,
                focus_keywords=[query],
            )
    except Exception as exc:
        return [], {"query": query, "status": "failed", "error": str(exc)}
    items = [
        {
            "title": getattr(item, "title", ""),
            "snippet": getattr(item, "snippet", ""),
            "url": getattr(item, "url", ""),
            "source": getattr(item, "source", ""),
            "published_date": getattr(item, "published_date", None),
        }
        for item in getattr(response, "results", []) or []
    ] if getattr(response, "success", False) else []
    return items, {
        "query": getattr(response, "query", query),
        "status": "ok" if getattr(response, "success", False) else "failed",
        "provider": getattr(response, "provider", ""),
        "count": len(items),
        **({"error": getattr(response, "error_message", None)} if getattr(response, "error_message", None) else {}),
    }


def _score_candidate_news_momentum(base_candidates: Iterable[Dict[str, Any]], *, limit: int) -> Dict[str, Any]:
    service = _build_search_service_for_candidates()
    if service is None or not getattr(service, "is_available", False):
        return {
            "status": "unavailable",
            "candidates": [],
            "diagnostics": [{"source": "search_service", "status": "unavailable", "reason": "No search provider configured"}],
        }

    scored_candidates: List[Dict[str, Any]] = []
    diagnostics: List[Dict[str, Any]] = []
    for item in list(base_candidates)[: max(1, min(12, limit * 2))]:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        name = _resolve_candidate_name(code, item.get("name") or item.get("stock_name"))
        news_items, diag = _stock_news_items_for_candidate(service, code, name)
        diag["code"] = code
        diagnostics.append(diag)
        scored = score_news_items(news_items)
        positive_count = int(scored.get("positive_count") or 0)
        negative_count = int(scored.get("negative_count") or 0)
        message_score = float(scored.get("message_score") or 0)
        if not news_items or positive_count <= 0 or negative_count > 0 or message_score < 58:
            continue
        event = (scored.get("events") or [{}])[0]
        payload = dict(item)
        payload["code"] = code
        payload["name"] = name
        payload["source"] = "news_momentum:company_event"
        payload["reason"] = "公司级新闻/公告事件形成消息面候选。"
        payload["signal_score"] = max(float(payload.get("signal_score") or 0), min(86.0, message_score + positive_count * 3.0))
        payload["message_score"] = int(message_score)
        payload["message_state"] = scored.get("message_state")
        payload["news_title"] = event.get("title")
        payload["news_source"] = event.get("source")
        payload["published_date"] = event.get("published_at")
        payload["event_tags"] = scored.get("event_tags") or []
        payload["risk_flags"] = [*(payload.get("risk_flags") or []), *(scored.get("risk_flags") or [])]
        payload["strategy_tags"] = list(dict.fromkeys([*(payload.get("strategy_tags") or []), "news_momentum"]))
        payload["metrics"] = {
            **(payload.get("metrics") or {}),
            "message_score": int(message_score),
            "positive_news_events": positive_count,
            "negative_news_events": negative_count,
        }
        scored_candidates.append(payload)

    merged = _dedupe_candidates(scored_candidates, limit)
    return {
        "status": "ok" if merged else ("empty" if diagnostics else "failed"),
        "candidates": merged,
        "diagnostics": diagnostics,
    }


_NEWS_MOMENTUM_QUERIES = (
    "A股 上市公司 公告 重大合同 中标 大订单 战略合作 业绩预增 回购 增持 最新",
    "A股 公司 订单 量产 新品 客户导入 国产替代 产业链 最新",
    "A股 上市公司 减持 问询函 处罚 立案 监管 风险 最新",
)


def _discover_news_momentum_candidates(
    *,
    limit: int,
    seed_candidates: Optional[Iterable[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    service = _build_search_service_for_candidates()
    if service is None or not getattr(service, "is_available", False):
        return {
            "status": "unavailable",
            "candidates": [],
            "queries": [],
            "diagnostics": [{"source": "search_service", "status": "unavailable", "reason": "No search provider configured"}],
        }

    candidates: List[Dict[str, Any]] = []
    queries: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()
    for query in _NEWS_MOMENTUM_QUERIES:
        try:
            response = _search_general_news(service, query, max_results=6, days=3)
        except Exception as exc:
            queries.append({"query": query, "status": "failed", "error": str(exc), "window_days": 3})
            continue
        results = getattr(response, "results", []) or []
        queries.append({
            "query": getattr(response, "query", query),
            "status": "ok" if getattr(response, "success", False) else "failed",
            "provider": getattr(response, "provider", ""),
            "count": len(results),
            "window_days": 3,
            **({"error": getattr(response, "error_message", None)} if getattr(response, "error_message", None) else {}),
        })
        if not getattr(response, "success", False):
            continue
        for result in results:
            url = str(getattr(result, "url", "") or "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            news_item = {
                "title": getattr(result, "title", ""),
                "snippet": getattr(result, "snippet", ""),
                "url": url,
                "source": getattr(result, "source", ""),
                "published_date": getattr(result, "published_date", None),
            }
            scored = score_news_items([news_item])
            if int(scored.get("positive_count") or 0) <= 0:
                continue
            if int(scored.get("negative_count") or 0) > 0:
                continue
            message_score = float(scored.get("message_score") or 0)
            if message_score < 58:
                continue
            event = (scored.get("events") or [{}])[0]
            text = f"{news_item['title']} {news_item['snippet']}"
            for code, name in _match_a_share_mentions(text, max_matches=3):
                candidates.append({
                    "code": code,
                    "name": name,
                    "source": "news_momentum:company_event",
                    "reason": "公司级新闻/公告事件形成消息面候选。",
                    "signal_score": min(88.0, message_score + 6.0),
                    "message_score": int(message_score),
                    "message_state": scored.get("message_state"),
                    "news_topic": query,
                    "news_title": event.get("title") or news_item["title"],
                    "news_snippet": news_item["snippet"],
                    "news_url": url,
                    "news_source": news_item["source"],
                    "published_date": event.get("published_at") or news_item["published_date"],
                    "event_tags": scored.get("event_tags") or [],
                    "strategy_tags": ["news_momentum"],
                    "metrics": {
                        "message_score": int(message_score),
                        "positive_news_events": int(scored.get("positive_count") or 0),
                        "negative_news_events": int(scored.get("negative_count") or 0),
                    },
                })
                if len(candidates) >= limit * 2:
                    break
            if len(candidates) >= limit * 2:
                break
        if len(candidates) >= limit * 2:
            break

    seed_result = _score_candidate_news_momentum(seed_candidates or [], limit=limit)
    candidates.extend(seed_result.get("candidates") or [])
    merged = _dedupe_candidates(candidates, limit)
    return {
        "status": "ok" if merged else ("empty" if any(item.get("status") == "ok" for item in queries) else "failed"),
        "candidates": merged,
        "queries": queries,
        "diagnostics": seed_result.get("diagnostics", []),
    }


def _search_general_news(service: Any, query: str, *, max_results: int, days: int):
    if hasattr(service, "search_general_news"):
        return service.search_general_news(query, max_results=max_results, days=days)
    return service.search_stock_news(
        stock_code="",
        stock_name="",
        max_results=max_results,
        focus_keywords=[query],
    )


def _event_rule_for_text(text: str) -> Optional[Dict[str, Any]]:
    haystack = str(text or "").lower()
    best: Optional[Dict[str, Any]] = None
    best_hits = 0
    for rule in _EVENT_IMPACT_RULES:
        hits = sum(1 for kw in rule["keywords"] if str(kw).lower() in haystack)
        if hits > best_hits:
            best = rule
            best_hits = hits
    return best if best_hits > 0 else None


def _event_id_from_title(title: str) -> str:
    text = re.sub(r"\s+", "_", str(title or "event").strip())[:80]
    text = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "", text)
    return text or "event"


def _event_cards_from_response(response: Any) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    for result in getattr(response, "results", []) or []:
        title = str(getattr(result, "title", "") or "").strip()
        snippet = str(getattr(result, "snippet", "") or "").strip()
        text = f"{title} {snippet}"
        rule = _event_rule_for_text(text)
        if not title or rule is None:
            continue
        cards.append({
            "event_id": _event_id_from_title(title),
            "title": title[:180],
            "snippet": snippet[:300],
            "event_type": rule["event_type"],
            "impact_variables": list(rule["variables"]),
            "watch_themes": list(rule["themes"]),
            "validation_terms": list(rule["validation_terms"]),
            "source": str(getattr(result, "source", "") or ""),
            "url": str(getattr(result, "url", "") or ""),
            "published_date": getattr(result, "published_date", None),
            "maturity": "breaking",
        })
    return cards


_VALIDATION_NEGATION_PREFIXES = (
    "暂无",
    "未见",
    "没有",
    "无",
    "尚无",
    "未出现",
    "尚未出现",
    "未发现",
    "尚未发现",
    "仍待",
    "待验证",
)


def _is_negated_validation_hit(text: str, term: str) -> bool:
    """Return True when a validation keyword is only mentioned as absent/unverified."""
    if not text or not term:
        return False
    term_text = str(term)
    seen = False
    text_value = str(text)
    boundaries = "。！？；;!?但不过然而"
    for match in re.finditer(re.escape(term_text), text_value):
        seen = True
        left = match.start()
        while left > 0 and text_value[left - 1] not in boundaries:
            left -= 1
        right = match.end()
        while right < len(text_value) and text_value[right] not in boundaries:
            right += 1
        clause = text_value[left:right]
        if not any(negation in clause for negation in _VALIDATION_NEGATION_PREFIXES):
            return False
    return seen


def _validation_matches_for_event(service: Any, event: Dict[str, Any], *, days: int = 7) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    for theme in event.get("watch_themes") or []:
        terms = " OR ".join(str(term) for term in (event.get("validation_terms") or [])[:4])
        query = f"{theme} {terms} 最新 进展 板块 异动 资金"
        try:
            response = _search_general_news(service, query, max_results=3, days=days)
        except Exception as exc:
            matches.append({"theme": theme, "query": query, "status": "failed", "error": str(exc)})
            continue
        results = []
        for item in getattr(response, "results", []) or []:
            text = f"{getattr(item, 'title', '')} {getattr(item, 'snippet', '')}"
            hit_terms = [
                term for term in event.get("validation_terms") or []
                if str(term) and str(term) in text and not _is_negated_validation_hit(text, str(term))
            ]
            if not hit_terms:
                continue
            results.append({
                "title": str(getattr(item, "title", "") or "")[:180],
                "snippet": str(getattr(item, "snippet", "") or "")[:220],
                "source": str(getattr(item, "source", "") or ""),
                "url": str(getattr(item, "url", "") or ""),
                "published_date": getattr(item, "published_date", None),
                "hit_terms": hit_terms[:4],
            })
        matches.append({
            "theme": theme,
            "query": getattr(response, "query", query),
            "status": "confirmed" if results else "watch_only",
            "provider": getattr(response, "provider", ""),
            "results": results,
        })
    return matches


def _candidates_from_confirmed_themes(events: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    per_theme_limit = max(2, min(5, limit))
    seen_themes: set[str] = set()
    for event in events:
        for match in event.get("validation_matches") or []:
            if match.get("status") != "confirmed":
                continue
            theme = str(match.get("theme") or "").strip()
            if not theme:
                continue
            if theme in seen_themes:
                continue
            if len(seen_themes) >= _EVENT_IMPACT_MAX_CONFIRMED_THEMES:
                logger.info(
                    "Event impact candidate theme cap reached (%d); remaining themes kept as watch-only context",
                    _EVENT_IMPACT_MAX_CONFIRMED_THEMES,
                )
                return _dedupe_candidates(candidates, limit) if candidates else []
            seen_themes.add(theme)
            for item in _fetch_sector_constituents(theme, limit=per_theme_limit):
                evidence = (match.get("results") or [{}])[0]
                payload = dict(item)
                payload["source"] = "event_impact:validated_theme"
                payload["reason"] = "事件传导验证后的主题成分候选。"
                payload["signal_score"] = max(float(payload.get("signal_score") or 0), 62.0)
                payload["event_title"] = event.get("title")
                payload["event_type"] = event.get("event_type")
                payload["impact_variables"] = event.get("impact_variables") or []
                payload["validated_theme"] = theme
                payload["validation_title"] = evidence.get("title")
                payload["validation_source"] = evidence.get("source")
                payload["validation_url"] = evidence.get("url")
                payload["strategy_tags"] = list(dict.fromkeys([*(payload.get("strategy_tags") or []), "event_impact"]))
                payload["metrics"] = {**(payload.get("metrics") or {}), "event_validation_hits": len(match.get("results") or [])}
                candidates.append(payload)
                if len(candidates) >= limit * 2:
                    break
            if len(candidates) >= limit * 2:
                break
        if len(candidates) >= limit * 2:
            break
    return _dedupe_candidates(candidates, limit)


def _ingest_event_watch_to_graphiti(events: List[Dict[str, Any]], *, market: str) -> None:
    if not events:
        return
    try:
        from src.config import get_config
        config = get_config()
        if not getattr(config, "graphiti_enabled", False):
            return
        from src.services.graphiti import get_graphiti_service
        service = get_graphiti_service()
        if not service.is_available():
            return
        for event in events[:8]:
            service.ingest_market_event_sync(
                event_id=str(event.get("event_id") or "event"),
                title=str(event.get("title") or "market event"),
                event_payload=event,
                market=market,
            )
    except Exception as exc:
        logger.debug("Graphiti market event ingest skipped: %s", exc)


def _discover_event_impact_candidates(*, market: str, limit: int) -> Dict[str, Any]:
    service = _build_search_service_for_candidates()
    if service is None or not getattr(service, "is_available", False):
        return {
            "status": "unavailable",
            "candidates": [],
            "events": [],
            "queries": [],
            "diagnostics": [{"source": "search_service", "status": "unavailable", "reason": "No search provider configured"}],
        }

    queries: List[Dict[str, Any]] = []
    events_by_id: Dict[str, Dict[str, Any]] = {}
    for query in _EVENT_IMPACT_QUERIES:
        try:
            response = _search_general_news(service, query, max_results=5, days=1)
        except Exception as exc:
            queries.append({"query": query, "status": "failed", "error": str(exc), "window_days": 1})
            continue
        cards = _event_cards_from_response(response)
        queries.append({
            "query": getattr(response, "query", query),
            "status": "ok" if getattr(response, "success", False) else "failed",
            "provider": getattr(response, "provider", ""),
            "count": len(getattr(response, "results", []) or []),
            "event_count": len(cards),
            "window_days": 1,
            **({"error": getattr(response, "error_message", None)} if getattr(response, "error_message", None) else {}),
        })
        for card in cards:
            events_by_id.setdefault(str(card["event_id"]), card)

    events = list(events_by_id.values())[:8]
    for event in events:
        validation_matches = _validation_matches_for_event(service, event, days=7)
        event["validation_window_days"] = 7
        event["validation_matches"] = validation_matches
        if any(match.get("status") == "confirmed" for match in validation_matches):
            event["maturity"] = "confirmed"
        elif validation_matches:
            event["maturity"] = "developing"
        else:
            event["maturity"] = "breaking"

    _ingest_event_watch_to_graphiti(events, market=market)
    candidates = _candidates_from_confirmed_themes(events, limit)
    status = "ok" if candidates else ("watch_only" if events else "empty")
    return {
        "status": status,
        "candidates": candidates,
        "events": events,
        "queries": queries,
        "diagnostics": [],
    }


# ============================================================
# get_market_indices
# ============================================================

def _handle_get_market_indices(region: str = "cn") -> dict:
    """Get major market indices."""
    manager = _get_fetcher_manager()
    timeout = _get_agent_timeout_attr("agent_regime_component_timeout_seconds", 2.0)
    indices, err, cost_ms = _run_with_timeout(
        lambda: manager.get_main_indices(region=region),
        timeout,
        "market_indices",
    )

    if err:
        return {
            "status": "timeout" if "timeout" in str(err).lower() else "failed",
            "region": region,
            "indices_count": 0,
            "indices": [],
            "source_chain": [{
                "provider": "market_indices",
                "result": "timeout" if "timeout" in str(err).lower() else "failed",
                "duration_ms": cost_ms,
            }],
            "errors": [str(err)],
            "error_summary": str(err),
        }

    if not indices:
        return {
            "status": "empty",
            "region": region,
            "indices_count": 0,
            "indices": [],
            "source_chain": [{
                "provider": "market_indices",
                "result": "empty",
                "duration_ms": cost_ms,
            }],
            "errors": [f"No market index data available for region '{region}'"],
        }

    return {
        "status": "ok",
        "region": region,
        "indices_count": len(indices),
        "indices": indices,
    }


get_market_indices_tool = ToolDefinition(
    name="get_market_indices",
    description="Get major market indices (e.g., Shanghai Composite, Shenzhen Component, "
                "CSI 300 for China; S&P 500, Nasdaq, Dow for US). Provides market overview.",
    parameters=[
        ToolParameter(
            name="region",
            type="string",
            description="Market region: 'cn' for China A-shares, 'hk' for Hong Kong, 'us' for US stocks (default: 'cn')",
            required=False,
            default="cn",
            enum=["cn", "hk", "us"],
        ),
    ],
    handler=_handle_get_market_indices,
    category="market",
)


# ============================================================
# get_sector_rankings
# ============================================================

def _handle_get_sector_rankings(top_n: int = 10) -> dict:
    """Get sector performance rankings."""
    timeout = max(3.0, _get_agent_timeout_attr("agent_sector_rankings_timeout_seconds", 10.0))
    started_at = time.time()
    tushare_timeout = min(timeout, max(1.0, min(4.5, timeout * 0.45)))
    tushare_result, tushare_err, tushare_ms = _run_with_timeout(
        lambda: _get_tushare_sector_rankings_fast(top_n, tushare_timeout),
        tushare_timeout,
        "tushare_ths_hot_industry_rankings",
    )
    if isinstance(tushare_result, tuple) and len(tushare_result) == 4:
        top_sectors, bottom_sectors, source_chain, chain_error = tushare_result
        if top_sectors or bottom_sectors:
            return {
                "status": "ok",
                "top_sectors": top_sectors or [],
                "bottom_sectors": bottom_sectors or [],
                "source_chain": source_chain,
                "data_source": "tushare:ths_hot",
                "ranking_market": "行业板块",
                "ranking_basis": "THS热榜:market=行业板块",
                "bottom_basis": "lowest pct_change within returned THS hot industry-board rows",
                "errors": [chain_error] if chain_error else [],
            }

    remaining_timeout = max(0.0, timeout - (time.time() - started_at))
    eastmoney_timeout = min(remaining_timeout, max(0.75, min(3.0, timeout * 0.30)))
    eastmoney_result, eastmoney_err, eastmoney_ms = _run_with_timeout(
        lambda: _get_eastmoney_industry_rankings_fast(top_n),
        eastmoney_timeout,
        "eastmoney_industry_rankings",
    )
    if isinstance(eastmoney_result, tuple) and len(eastmoney_result) == 4:
        top_sectors, bottom_sectors, source_chain, chain_error = eastmoney_result
        if top_sectors or bottom_sectors:
            return {
                "status": "ok",
                "top_sectors": top_sectors or [],
                "bottom_sectors": bottom_sectors or [],
                "source_chain": source_chain,
                "errors": [chain_error] if chain_error else [],
            }

    remaining_timeout = max(0.0, timeout - (time.time() - started_at))
    stockapi_timeout = min(remaining_timeout, max(0.5, min(2.5, timeout * 0.25)))
    stockapi_result, stockapi_err, stockapi_ms = _run_with_timeout(
        lambda: _get_stockapi_sector_rankings_fast(top_n),
        stockapi_timeout,
        "stockapi_hot_sectors",
    )
    if isinstance(stockapi_result, tuple) and len(stockapi_result) == 4:
        top_sectors, bottom_sectors, source_chain, chain_error = stockapi_result
        if top_sectors or bottom_sectors:
            return {
                "status": "ok",
                "top_sectors": top_sectors or [],
                "bottom_sectors": bottom_sectors or [],
                "source_chain": source_chain,
                "errors": [chain_error] if chain_error else [],
            }

    source_chain: List[Dict[str, Any]] = []
    errors: List[str] = []
    if tushare_err:
        source_chain.append({
            "provider": "tushare:ths_hot",
            "result": "timeout" if "timeout" in str(tushare_err).lower() else "failed",
            "duration_ms": tushare_ms,
            "market": "行业板块",
            "error": tushare_err,
        })
        errors.append(str(tushare_err))
    elif isinstance(tushare_result, tuple) and len(tushare_result) == 4:
        _, _, tushare_chain, tushare_chain_error = tushare_result
        source_chain.extend(tushare_chain or [])
        if tushare_chain_error:
            errors.append(str(tushare_chain_error))
    else:
        source_chain.append({
            "provider": "tushare:ths_hot",
            "result": "empty",
            "duration_ms": tushare_ms,
            "market": "行业板块",
        })

    if eastmoney_err:
        source_chain.append({
            "provider": "eastmoney:stock_board_industry_name_em",
            "result": "timeout" if "timeout" in str(eastmoney_err).lower() else "failed",
            "duration_ms": eastmoney_ms,
            "error": eastmoney_err,
        })
        errors.append(str(eastmoney_err))
    elif isinstance(eastmoney_result, tuple) and len(eastmoney_result) == 4:
        _, _, eastmoney_chain, eastmoney_chain_error = eastmoney_result
        source_chain.extend(eastmoney_chain or [])
        if eastmoney_chain_error:
            errors.append(str(eastmoney_chain_error))
    else:
        source_chain.append({
            "provider": "eastmoney:stock_board_industry_name_em",
            "result": "empty",
            "duration_ms": eastmoney_ms,
        })

    if stockapi_err:
        source_chain.append({
            "provider": "stockapi:hotBkJlrDr",
            "result": "timeout" if "timeout" in str(stockapi_err).lower() else "failed",
            "duration_ms": stockapi_ms,
            "error": stockapi_err,
        })
        errors.append(str(stockapi_err))
    elif isinstance(stockapi_result, tuple) and len(stockapi_result) == 4:
        _, _, stockapi_chain, stockapi_chain_error = stockapi_result
        for item in stockapi_chain or []:
            if isinstance(item, dict):
                source_chain.append(item)
            else:
                source_chain.append({"provider": str(item), "result": "failed", "duration_ms": stockapi_ms})
        if stockapi_chain_error:
            if not source_chain:
                source_chain.append({
                    "provider": "stockapi:hotBkJlrDr",
                    "result": "failed",
                    "duration_ms": stockapi_ms,
                    "error": str(stockapi_chain_error),
                })
            errors.append(str(stockapi_chain_error))
    else:
        source_chain.append({
            "provider": "stockapi:hotBkJlrDr",
            "result": "empty",
            "duration_ms": stockapi_ms,
        })

    if not source_chain:
        source_chain.append({
            "provider": "stockapi:hotBkJlrDr",
            "result": "empty",
            "duration_ms": stockapi_ms,
        })

    elapsed_ms = int((time.time() - started_at) * 1000)
    if any("timeout" in str(error).lower() for error in errors):
        return {
            "status": "timeout",
            "top_sectors": [],
            "bottom_sectors": [],
            "source_chain": source_chain,
            "errors": errors or ["sector_rankings timeout"],
            "error_summary": " | ".join(errors) or "sector_rankings timeout",
            "duration_ms": elapsed_ms,
        }
    return {
        "status": "failed" if errors else "empty",
        "top_sectors": [],
        "bottom_sectors": [],
        "source_chain": source_chain,
        "errors": errors or ["No sector ranking data available"],
        "error_summary": " | ".join(errors) if errors else "No sector ranking data available",
        "duration_ms": elapsed_ms,
    }


get_sector_rankings_tool = ToolDefinition(
    name="get_sector_rankings",
    description=(
        "Get industry-board hot rankings. Prefer Tushare THS hot rank "
        "`ths_hot(market=行业板块)`; fall back to Eastmoney industry-board realtime "
        "quotes (stock_board_industry_name_em / quote.eastmoney boardlist page), then "
        "StockAPI /v1/hotBkJlrDr when available. Permission, quota, network and remote "
        "disconnect failures are returned as structured failed/timeout diagnostics "
        "without slow fetcher probing."
    ),
    parameters=[
        ToolParameter(
            name="top_n",
            type="integer",
            description="Number of top/bottom sectors to return (default: 10)",
            required=False,
            default=10,
        ),
    ],
    handler=_handle_get_sector_rankings,
    category="market",
)


# ============================================================
# detect_market_regime
# ============================================================

def _latest_numeric(items: Iterable[Dict[str, Any]], *keys: str) -> Optional[float]:
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for key in keys:
            if key not in item:
                continue
            try:
                value = float(item.get(key))
            except Exception:
                continue
            return value
    return None


def _pct_change(values: List[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    first = values[-2]
    last = values[-1]
    if not first:
        return None
    return max(-1.0, min(1.0, (last - first) / abs(first)))


def _derive_market_breadth(indices: List[Dict[str, Any]]) -> Optional[float]:
    changes: List[float] = []
    for item in indices or []:
        try:
            changes.append(float(item.get("change_pct")))
        except Exception:
            continue
    if not changes:
        return None
    positive = sum(1 for value in changes if value > 0)
    negative = sum(1 for value in changes if value < 0)
    total = positive + negative
    if total <= 0:
        return 0.0
    return max(-1.0, min(1.0, (positive - negative) / total))


def _derive_northbound_component(flow: Dict[str, Any]) -> Optional[float]:
    history = flow.get("history") or []
    values: List[float] = []
    for row in history:
        value = _latest_numeric([row], "net_inflow", "净买入", "northbound_net_inflow")
        if value is not None:
            values.append(value)
    if values:
        scale = max(abs(v) for v in values[-10:]) or 1.0
        return max(-1.0, min(1.0, values[-1] / scale))
    summary = flow.get("summary") or {}
    value = _latest_numeric([summary], "northbound_net_inflow", "北向资金")
    if value is None:
        return None
    return max(-1.0, min(1.0, value / 10_000_000_000.0))


def _derive_margin_component(margin: Dict[str, Any]) -> Optional[float]:
    rows = list(margin.get("sse") or []) + list(margin.get("szse") or [])
    balances: List[float] = []
    for row in rows:
        value = _latest_numeric([row], "margin_balance", "融资余额")
        if value is not None:
            balances.append(value)
    return _pct_change(balances[-2:]) if balances else None


def _derive_market_flow_component(flow: Dict[str, Any]) -> Optional[float]:
    market_flow = flow.get("market_flow") or {}
    value = _latest_numeric([market_flow], "main_net_inflow", "主力净流入")
    if value is None:
        return None
    return max(-1.0, min(1.0, value / 50_000_000_000.0))


def _load_market_history(
    index_code: str,
    lookback_days: int,
    *,
    cache_first: bool = False,
) -> tuple[List[Dict[str, Any]], str]:
    ts_code = _normalize_index_ts_code(index_code)
    if cache_first:
        cached_rows, cached_source = _load_market_history_cache_only(
            ts_code,
            lookback_days,
            source_suffix="cache_first",
        )
        if cached_rows:
            return cached_rows, cached_source

    fast_rows, fast_source = _load_index_history_from_tushare(index_code, lookback_days)
    if fast_rows:
        _store_market_history_run_cache(_normalize_index_ts_code(index_code), fast_rows, fast_source)
        return fast_rows, fast_source

    from src.services.history_loader import load_history_df

    fallback_to_network = not _is_supported_cn_index_proxy(index_code)
    df, source = load_history_df(index_code, days=lookback_days, fallback_to_network=fallback_to_network)
    if df is not None and not df.empty:
        rows = df.tail(lookback_days).to_dict(orient="records")
        for row in rows:
            if "date" in row:
                row["date"] = str(row["date"])
        _store_market_history_run_cache(ts_code, rows, source)
        return rows, source

    sdk_source = ""
    if fallback_to_network:
        sdk_rows, sdk_source = _load_index_history_from_tushare_sdk(index_code, lookback_days)
        if sdk_rows:
            _store_market_history_run_cache(ts_code, sdk_rows, sdk_source)
            return sdk_rows, sdk_source

    sources = [s for s in [fast_source, source, sdk_source] if s]
    return [], ";".join(sources) if sources else "all_sources_failed"


def _load_market_history_cache_only(
    ts_code: str,
    lookback_days: int,
    *,
    source_suffix: str,
) -> tuple[List[Dict[str, Any]], str]:
    run_rows, run_source = _load_market_history_run_cache(ts_code, lookback_days, source_suffix=source_suffix)
    if run_rows:
        return run_rows, run_source
    disk_rows, disk_source = _load_index_history_cache(ts_code, lookback_days, source_suffix=source_suffix)
    if disk_rows:
        _store_market_history_run_cache(ts_code, disk_rows, disk_source)
        return disk_rows, disk_source
    return [], disk_source


def _load_market_history_run_cache(
    ts_code: str,
    lookback_days: int,
    *,
    source_suffix: str,
) -> tuple[List[Dict[str, Any]], str]:
    now = time.time()
    with _MARKET_HISTORY_RUN_CACHE_LOCK:
        cached = _MARKET_HISTORY_RUN_CACHE.get(ts_code)
    if not cached:
        return [], f"run_cache_miss:{source_suffix}"
    cached_at, rows, source = cached
    if now - cached_at > _MARKET_HISTORY_RUN_CACHE_TTL_SECONDS:
        with _MARKET_HISTORY_RUN_CACHE_LOCK:
            _MARKET_HISTORY_RUN_CACHE.pop(ts_code, None)
        return [], f"run_cache_expired:{source_suffix}"
    normalized_rows = [row for row in rows if isinstance(row, dict)]
    normalized_rows = sorted(normalized_rows, key=lambda item: str(item.get("date") or ""))
    if len(normalized_rows) < int(max(1, lookback_days)):
        return [], f"run_cache_short:{source_suffix}:{len(normalized_rows)}"
    return normalized_rows[-int(max(1, lookback_days)):], f"{source}:run_cache:{source_suffix}"


def _store_market_history_run_cache(ts_code: str, rows: List[Dict[str, Any]], source: str) -> None:
    normalized_rows = [row for row in rows if isinstance(row, dict)]
    if not normalized_rows:
        return
    normalized_rows = sorted(normalized_rows, key=lambda item: str(item.get("date") or ""))
    with _MARKET_HISTORY_RUN_CACHE_LOCK:
        _MARKET_HISTORY_RUN_CACHE[ts_code] = (time.time(), normalized_rows, source or "market_history")
        if len(_MARKET_HISTORY_RUN_CACHE) > _MARKET_HISTORY_RUN_CACHE_MAX_ITEMS:
            oldest_key = min(
                _MARKET_HISTORY_RUN_CACHE,
                key=lambda key: _MARKET_HISTORY_RUN_CACHE[key][0],
            )
            _MARKET_HISTORY_RUN_CACHE.pop(oldest_key, None)


def _load_index_history_from_tushare(index_code: str, lookback_days: int) -> tuple[List[Dict[str, Any]], str]:
    ts_code = _normalize_index_ts_code(index_code)
    try:
        from data_provider.tushare_client import build_tushare_http_client, get_tushare_token
    except Exception:
        return _load_index_history_cache(ts_code, lookback_days, source_suffix="import_unavailable")
    if not get_tushare_token():
        return _load_index_history_cache(ts_code, lookback_days, source_suffix="no_token")

    end_day = datetime.now().date()
    start_day = end_day - timedelta(days=int(max(lookback_days, 120) * 1.8) + 10)
    try:
        client = build_tushare_http_client(
            timeout=_get_agent_timeout_attr("agent_tushare_tool_timeout_seconds", 20.0)
        )
        df = client.query(
            "index_daily",
            fields="ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
            ts_code=ts_code,
            start_date=start_day.strftime("%Y%m%d"),
            end_date=end_day.strftime("%Y%m%d"),
        )
    except Exception as exc:
        logger.warning("Tushare HTTP index_daily failed for %s: %s", ts_code, exc)
        cached_rows, cached_source = _load_index_history_cache(ts_code, lookback_days, source_suffix="after_http_error")
        if cached_rows:
            return cached_rows, cached_source
        return [], "tushare:index_daily_failed"
    if df is None or df.empty:
        cached_rows, cached_source = _load_index_history_cache(ts_code, lookback_days, source_suffix="after_empty")
        if cached_rows:
            return cached_rows, cached_source
        return [], "tushare:index_daily_empty"

    rows: List[Dict[str, Any]] = []
    for row in df.to_dict(orient="records"):
        trade_date = str(row.get("trade_date") or "")
        if len(trade_date) == 8 and trade_date.isdigit():
            trade_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
        rows.append({
            "date": trade_date,
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
            "volume": row.get("vol"),
            "amount": row.get("amount"),
            "pct_chg": row.get("pct_chg"),
        })
    rows = sorted(rows, key=lambda item: str(item.get("date") or ""))
    rows = rows[-lookback_days:]
    _write_index_history_cache(ts_code, rows, source="tushare:index_daily")
    return rows, "tushare:index_daily"


def _index_history_cache_path(ts_code: str) -> Path:
    safe_code = re.sub(r"[^A-Z0-9_.-]", "_", str(ts_code or "index").upper())
    return Path(".cache") / "regime" / "index_daily" / f"{safe_code}.json"


def _load_index_history_cache(ts_code: str, lookback_days: int, *, source_suffix: str = "hit") -> tuple[List[Dict[str, Any]], str]:
    path = _index_history_cache_path(ts_code)
    if not path.exists():
        return [], f"tushare:index_daily_cache_miss:{source_suffix}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("rows") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return [], f"tushare:index_daily_cache_invalid:{source_suffix}"
        normalized_rows = [row for row in rows if isinstance(row, dict)]
        normalized_rows = sorted(normalized_rows, key=lambda item: str(item.get("date") or ""))
        if not normalized_rows:
            return [], f"tushare:index_daily_cache_empty:{source_suffix}"
        return normalized_rows[-int(max(1, lookback_days)):], f"tushare:index_daily_cache:{source_suffix}"
    except Exception as exc:
        logger.warning("Tushare index_daily cache read failed for %s: %s", ts_code, exc)
        return [], f"tushare:index_daily_cache_error:{source_suffix}"


def _write_index_history_cache(ts_code: str, rows: List[Dict[str, Any]], *, source: str) -> None:
    if not rows:
        return
    path = _index_history_cache_path(ts_code)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts_code": ts_code,
            "source": source,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "rows": rows,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    except Exception as exc:
        logger.debug("Tushare index_daily cache write skipped for %s: %s", ts_code, exc)


def _load_index_history_from_tushare_sdk(index_code: str, lookback_days: int) -> tuple[List[Dict[str, Any]], str]:
    """Fallback: use tushare SDK (pro_api) when the HTTP client fails."""
    try:
        from data_provider.tushare_client import build_tushare_sdk_client
    except Exception:
        return [], ""

    ts_code = _normalize_index_ts_code(index_code)
    end_day = datetime.now().date()
    start_day = end_day - timedelta(days=int(max(lookback_days, 120) * 1.8) + 10)
    try:
        _ts, pro = build_tushare_sdk_client()
        df = pro.index_daily(
            ts_code=ts_code,
            start_date=start_day.strftime("%Y%m%d"),
            end_date=end_day.strftime("%Y%m%d"),
        )
    except Exception as exc:
        logger.warning("Tushare SDK index_daily failed for %s: %s", ts_code, exc)
        return [], "tushare_sdk:index_daily_failed"
    if df is None or df.empty:
        return [], "tushare_sdk:index_daily_empty"

    rows: List[Dict[str, Any]] = []
    for row in df.to_dict(orient="records"):
        trade_date = str(row.get("trade_date") or "")
        if len(trade_date) == 8 and trade_date.isdigit():
            trade_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
        rows.append({
            "date": trade_date,
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
            "volume": row.get("vol"),
            "amount": row.get("amount"),
            "pct_chg": row.get("pct_chg"),
        })
    rows = sorted(rows, key=lambda item: str(item.get("date") or ""))
    return rows[-lookback_days:], "tushare_sdk:index_daily"


def _normalize_index_ts_code(index_code: str) -> str:
    raw = str(index_code or "000300").strip().upper()
    if raw.endswith((".SH", ".SZ")):
        return raw
    if raw.startswith("SH"):
        return f"{raw[2:]}.SH"
    if raw.startswith("SZ"):
        return f"{raw[2:]}.SZ"
    code = re.sub(r"\D", "", raw) or "000300"
    sh_indices = {"000001", "000016", "000300", "000688", "000852", "000905"}
    if code in sh_indices:
        return f"{code}.SH"
    return f"{code}.SZ"


def _is_supported_cn_index_proxy(index_code: str) -> bool:
    ts_code = _normalize_index_ts_code(index_code)
    return ts_code in {
        "000001.SH",
        "000016.SH",
        "000300.SH",
        "000688.SH",
        "000852.SH",
        "000905.SH",
        "399001.SZ",
        "399006.SZ",
    }


def _handle_detect_market_regime(
    market: str = "cn",
    index_code: str = "000300",
    lookback_days: int = 260,
    confirmation_bars: int = 3,
    persist: bool = True,
) -> dict:
    """Detect A-share market regime from OHLCV, flow, breadth and persisted state."""
    market_key = (market or "cn").strip().lower()
    if market_key != "cn":
        return {
            "status": "not_supported",
            "market": market_key,
            "error": "detect_market_regime currently supports China A-share market only.",
        }

    lookback = max(120, min(int(lookback_days or 260), 520))
    component_timeout = _get_agent_timeout_attr("agent_regime_component_timeout_seconds", 25.0)
    auxiliary_timeout = max(3.0, component_timeout)
    history_result, history_err, history_ms = _run_with_timeout(
        lambda: _load_market_history(index_code or "000300", lookback),
        component_timeout,
        "market_history",
    )
    data_errors: List[str] = []
    if isinstance(history_result, tuple) and len(history_result) == 2:
        history_rows, history_source = history_result
    else:
        history_rows, history_source = [], "timeout" if history_err else "none"
        if history_err:
            data_errors.append(f"market_history: {history_err}")
    bars = coerce_bars(history_rows)
    if not bars and not history_err and history_source not in {"", "none"}:
        data_errors.append(f"market_history: {history_source}")
    try:
        from src.storage import get_db

        db = get_db()
        previous = db.get_market_regime_state(market_key) or {}
    except Exception as exc:
        logger.warning("Failed to read market regime state: %s", exc)
        db = None
        previous = {}

    indices = []
    sector_rankings: Dict[str, Any] = {}
    northbound: Dict[str, Any] = {}
    margin: Dict[str, Any] = {}
    market_flow: Dict[str, Any] = {}

    component_tasks: Dict[str, Tuple[Callable[[], Any], float]] = {
        "market_indices": (lambda: _handle_get_market_indices(region="cn"), auxiliary_timeout),
        "sector_rankings": (lambda: _handle_get_sector_rankings(top_n=10), auxiliary_timeout),
    }
    try:
        from src.agent.tools.data_tools import (
            _handle_get_market_capital_flow,
            _handle_get_margin_trading_summary,
            _handle_get_northbound_capital_flow,
        )

        component_tasks.update({
            "market_flow": (lambda: _handle_get_market_capital_flow(top_n=5), auxiliary_timeout),
            "northbound": (lambda: _handle_get_northbound_capital_flow(limit=10), auxiliary_timeout),
            "margin": (lambda: _handle_get_margin_trading_summary(limit=10), auxiliary_timeout),
        })
    except Exception as exc:
        data_errors.append(f"capital_flow_components_import: {exc}")

    component_diagnostics: Dict[str, Any] = {
        "market_history": {
            "status": "ok" if bars else ("timeout" if history_err else "empty"),
            "duration_ms": history_ms,
            "error": history_err,
        }
    }
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(component_tasks)))
    try:
        futures = {
            pool.submit(_run_with_timeout, task, timeout_s, name): name
            for name, (task, timeout_s) in component_tasks.items()
        }
        done, pending = concurrent.futures.wait(
            futures,
            timeout=max(timeout_s for _task, timeout_s in component_tasks.values()) + 0.5,
            return_when=concurrent.futures.ALL_COMPLETED,
        )
        for future in pending:
            name = futures[future]
            timeout_s = component_tasks[name][1]
            future.cancel()
            component_diagnostics[name] = {
                "status": "timeout",
                "duration_ms": int(timeout_s * 1000),
                "error": f"{name} timeout",
            }
            data_errors.append(f"{name}: {name} timeout")
        for future in done:
            name = futures[future]
            try:
                payload, err, cost_ms = future.result()
            except Exception as exc:
                payload, err, cost_ms = None, str(exc), 0
            status = "ok" if err is None else ("timeout" if "timeout" in str(err).lower() else "failed")
            component_diagnostics[name] = {"status": status, "duration_ms": cost_ms, "error": err}
            if err:
                data_errors.append(f"{name}: {err}")
                continue
            if not isinstance(payload, dict):
                data_errors.append(f"{name}: invalid payload")
                continue
            payload_status = str(payload.get("status", "")).lower()
            if payload_status in {"failed", "error", "timeout"}:
                errors = payload.get("errors") or payload.get("error") or payload.get("error_summary")
                data_errors.append(f"{name}: {errors}")
            if name == "market_indices":
                indices = payload.get("indices") or []
            elif name == "sector_rankings":
                sector_rankings = payload
            elif name == "market_flow":
                market_flow = payload
            elif name == "northbound":
                northbound = payload
            elif name == "margin":
                margin = payload
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    sentiment = SentimentComponents(
        margin_balance_change=_derive_margin_component(margin),
        market_breadth=_derive_market_breadth(indices),
        fear_greed_index=None,
        northbound_flow_z=_derive_northbound_component(northbound),
        market_flow_z=_derive_market_flow_component(market_flow),
        raw={
            "indices_count": len(indices),
            "top_sectors": (sector_rankings.get("top_sectors") or sector_rankings.get("sectors") or [])[:5],
            "component_sources": {
                "northbound": northbound.get("source_chain"),
                "margin": margin.get("source_chain"),
                "market_flow": market_flow.get("source_chain"),
            },
        },
    )

    state = detect_market_regime(
        bars,
        market=market_key,
        sentiment=sentiment,
        previous_bucket=(previous.get("payload") or {}).get("volatility_bucket") or previous.get("volatility_bucket"),
        previous_regime=previous.get("regime"),
        pending_regime=previous.get("pending_regime"),
        pending_count=int(previous.get("pending_count") or 0),
        confirmation_bars=confirmation_bars,
    )
    payload = state.to_dict()
    payload.update({
        "status": "ok" if state.data_quality != "insufficient" else "insufficient_data",
        "index_code": index_code,
        "history_source": history_source,
        "history_records": len(bars),
        "persisted": False,
        "data_errors": data_errors,
        "component_diagnostics": component_diagnostics,
        "market_context": {
            "indices": indices,
            "sector_rankings": {
                "top": (sector_rankings.get("top_sectors") or sector_rankings.get("sectors") or [])[:5],
                "bottom": (sector_rankings.get("bottom_sectors") or [])[:5],
            },
        },
    })

    if payload["status"] == "insufficient_data" and previous:
        prev_regime = previous.get("regime") or (previous.get("payload") or {}).get("regime")
        if prev_regime:
            prev_payload = previous.get("payload") or previous
            payload["status"] = "stale_fallback"
            payload["regime"] = prev_regime
            for key in ("volatility_bucket", "atr_percentile", "wyckoff_phase",
                        "sentiment_score", "composite_score", "position_guidance"):
                if key in prev_payload and (key not in payload or payload.get(key) is None):
                    payload[key] = prev_payload[key]
            payload["stale_source"] = "persisted_state"
            payload["stale_note"] = (
                "Fresh market data unavailable; returning last persisted regime. "
                "Treat position_guidance as conservative until fresh data confirms."
            )
            logger.warning(
                "detect_market_regime: insufficient fresh data, falling back to persisted state "
                "(regime=%s, persisted_at=%s)",
                prev_regime,
                previous.get("updated_at") or previous.get("timestamp") or "unknown",
            )

    if persist and db is not None and payload["status"] not in ("insufficient_data", "stale_fallback"):
        try:
            db.save_market_regime_state(market_key, payload)
            payload["persisted"] = True
        except Exception as exc:
            logger.warning("Failed to persist market regime state: %s", exc)
            payload.setdefault("data_errors", []).append(f"persist: {exc}")

    return payload


detect_market_regime_tool = ToolDefinition(
    name="detect_market_regime",
    description=(
        "Detect China A-share market regime using empirical ATR percentile, damped volatility "
        "bucket, A-share sentiment/liquidity substitutes, Wyckoff phase, and persisted "
        "confirmation state. Use as the default pre-check for entry, position review, and "
        "watchlist selection."
    ),
    parameters=[
        ToolParameter(
            name="market",
            type="string",
            description="Market code. Currently only 'cn' is supported.",
            required=False,
            default="cn",
            enum=["cn"],
        ),
        ToolParameter(
            name="index_code",
            type="string",
            description="A-share index proxy for regime OHLCV, default CSI 300 '000300'.",
            required=False,
            default="000300",
        ),
        ToolParameter(
            name="lookback_days",
            type="integer",
            description="Trading days for empirical CDF and Wyckoff analysis (default 260, max 520).",
            required=False,
            default=260,
        ),
        ToolParameter(
            name="confirmation_bars",
            type="integer",
            description="Number of consecutive confirmations required before switching persisted regime.",
            required=False,
            default=3,
        ),
        ToolParameter(
            name="persist",
            type="boolean",
            description="Persist the latest regime state to local SQLite for damping/confirmation.",
            required=False,
            default=True,
        ),
    ],
    handler=_handle_detect_market_regime,
    category="market",
)


def _handle_get_regime_forward_probability(
    market: str = "cn",
    index_code: str = "000300",
    regime: Optional[str] = None,
    current_price: Optional[float] = None,
    lookback_days: int = 900,
    windows: Optional[List[int]] = None,
) -> dict:
    """Compute forward-return probabilities for the current or requested regime."""
    market_key = (market or "cn").strip().lower()
    if market_key != "cn":
        return {
            "status": "not_supported",
            "market": market_key,
            "error": "get_regime_forward_probability currently supports China A-share market only.",
        }

    normalized_windows = [
        int(value)
        for value in (windows or [7, 30, 60, 90])
        if str(value).strip().isdigit() and int(value) > 0
    ] or [7, 30, 60, 90]
    max_window = max(normalized_windows)
    lookback = max(260, min(int(lookback_days or 900), 1600))
    history_result, history_err, history_ms = _run_with_timeout(
        lambda: _load_market_history(
            index_code or "000300",
            lookback + max_window + 100,
            cache_first=True,
        ),
        _get_agent_timeout_attr("agent_regime_component_timeout_seconds", 25.0),
        "regime_forward_history",
    )
    if isinstance(history_result, tuple) and len(history_result) == 2:
        history_rows, history_source = history_result
    else:
        history_rows, history_source = [], "timeout" if history_err else "none"
    bars = coerce_bars(history_rows)
    if len(bars) < 140 + max_window:
        return {
            "status": "insufficient_data",
            "market": market_key,
            "index_code": index_code,
            "history_source": history_source,
            "history_records": len(bars),
            "duration_ms": history_ms,
            "error": history_err or "not enough historical OHLC bars for forward probability.",
            "note": "样本不足，不生成强概率结论。",
        }

    current_state = detect_market_regime(bars, market=market_key, confirmation_bars=1).to_dict()
    target_regime = str(regime or current_state.get("regime") or "unknown").strip().lower()
    frame = compute_regime_return_frame(
        bars,
        windows=normalized_windows,
        market=market_key,
    )
    probability = build_regime_probability(frame, regime=target_regime, windows=normalized_windows)
    sample_quality_summary = build_sample_quality_summary(bars, frame, windows=normalized_windows)
    profile_window = 90 if 90 in normalized_windows else max_window
    path_profile = build_path_profile(frame, regime=target_regime, window=profile_window)
    price = current_price
    if price is None and bars:
        price = bars[-1].close
    reentry = build_reentry_reference(
        probability,
        current_price=price,
        window=30 if 30 in normalized_windows else min(normalized_windows),
    )
    status = "ok" if probability.get("sample_count", 0) else "insufficient_data"
    payload = {
        "status": status,
        "market": market_key,
        "index_code": index_code,
        "symbol": _normalize_index_ts_code(index_code),
        "as_of": current_state.get("as_of"),
        "regime": target_regime,
        "current_regime": current_state,
        "source": "ohlc_forward_return",
        "history_source": history_source,
        "history_records": len(bars),
        "duration_ms": history_ms,
        "windows": probability.get("windows", {}),
        "sample_count": probability.get("sample_count", 0),
        "sample_quality_summary": sample_quality_summary,
        "path_profile": path_profile,
        "reentry_reference": reentry,
        "brief": "",
        "guardrails": [
            "Regime probability is evidence only and must not directly override Judge or Risk Gate.",
            "low_confidence=true means the probability layer cannot be a primary decision reason.",
        ],
    }
    payload["brief"] = format_regime_probability_brief(payload)
    if history_err:
        payload.setdefault("data_errors", []).append(f"history: {history_err}")
    return payload


def _load_symbol_history(stock_code: str, lookback_days: int) -> tuple[List[Dict[str, Any]], str]:
    from src.services.history_loader import load_history_df

    df, source = load_history_df(stock_code, days=lookback_days, fallback_to_network=True)
    if df is None or df.empty:
        return [], source or "none"
    rows = df.tail(lookback_days).to_dict(orient="records")
    for row in rows:
        if "date" in row:
            row["date"] = str(row["date"])[:10]
    return rows, source


def _handle_get_symbol_regime_probability(
    stock_code: str,
    market: str = "cn",
    index_code: str = "000300",
    regime: Optional[str] = None,
    current_price: Optional[float] = None,
    lookback_days: int = 900,
    windows: Optional[List[int]] = None,
) -> dict:
    """Compute single-symbol forward probabilities conditional on market regime."""
    market_key = (market or "cn").strip().lower()
    if market_key != "cn":
        return {
            "status": "not_supported",
            "market": market_key,
            "error": "get_symbol_regime_probability currently supports China A-share market only.",
        }
    symbol = str(stock_code or "").strip()
    if not symbol:
        return {"status": "invalid_input", "error": "stock_code is required."}

    normalized_windows = [
        int(value)
        for value in (windows or [7, 30, 60, 90])
        if str(value).strip().isdigit() and int(value) > 0
    ] or [7, 30, 60, 90]
    max_window = max(normalized_windows)
    lookback = max(260, min(int(lookback_days or 900), 1600))
    history_days = lookback + max_window + 100
    component_timeout = _get_agent_timeout_attr("agent_regime_component_timeout_seconds", 25.0)

    market_result, market_err, market_ms = _run_with_timeout(
        lambda: _load_market_history(index_code or "000300", history_days, cache_first=True),
        component_timeout,
        "symbol_regime_market_history",
    )
    if isinstance(market_result, tuple) and len(market_result) == 2:
        market_rows, market_source = market_result
    else:
        market_rows, market_source = [], "timeout" if market_err else "none"
    market_bars = coerce_bars(market_rows)

    symbol_result, symbol_err, symbol_ms = _run_with_timeout(
        lambda: _load_symbol_history(symbol, history_days),
        component_timeout,
        "symbol_regime_symbol_history",
    )
    if isinstance(symbol_result, tuple) and len(symbol_result) == 2:
        symbol_rows, symbol_source = symbol_result
    else:
        symbol_rows, symbol_source = [], "timeout" if symbol_err else "none"
    symbol_bars = coerce_bars(symbol_rows)

    if len(market_bars) < 140 + max_window or len(symbol_bars) < 140 + max_window:
        return {
            "status": "insufficient_data",
            "market": market_key,
            "stock_code": symbol,
            "index_code": index_code,
            "market_history_source": market_source,
            "symbol_history_source": symbol_source,
            "market_history_records": len(market_bars),
            "symbol_history_records": len(symbol_bars),
            "duration_ms": market_ms + symbol_ms,
            "errors": [item for item in [market_err, symbol_err] if item],
            "note": "样本不足，不生成单股 regime probability。",
        }

    current_state = detect_market_regime(market_bars, market=market_key, confirmation_bars=1).to_dict()
    target_regime = str(regime or current_state.get("regime") or "unknown").strip().lower()
    market_frame = compute_regime_return_frame(
        market_bars,
        windows=normalized_windows,
        market=market_key,
    )
    symbol_frame = compute_symbol_regime_return_frame(
        symbol_bars,
        market_frame,
        windows=normalized_windows,
    )
    probability = build_regime_probability(symbol_frame, regime=target_regime, windows=normalized_windows)
    sample_quality_summary = build_sample_quality_summary(symbol_bars, symbol_frame, windows=normalized_windows)
    profile_window = 90 if 90 in normalized_windows else max_window
    path_profile = build_path_profile(symbol_frame, regime=target_regime, window=profile_window)
    price = current_price
    if price is None and symbol_bars:
        price = symbol_bars[-1].close
    reentry = build_reentry_reference(
        probability,
        current_price=price,
        window=30 if 30 in normalized_windows else min(normalized_windows),
    )
    status = "ok" if probability.get("sample_count", 0) else "insufficient_data"
    payload = {
        "status": status,
        "market": market_key,
        "stock_code": symbol,
        "index_code": index_code,
        "index_symbol": _normalize_index_ts_code(index_code),
        "as_of": current_state.get("as_of"),
        "regime": target_regime,
        "current_regime": current_state,
        "source": "symbol_ohlc_forward_return_by_market_regime",
        "market_history_source": market_source,
        "symbol_history_source": symbol_source,
        "market_history_records": len(market_bars),
        "symbol_history_records": len(symbol_bars),
        "matched_anchor_count": len(symbol_frame),
        "duration_ms": market_ms + symbol_ms,
        "windows": probability.get("windows", {}),
        "sample_count": probability.get("sample_count", 0),
        "sample_quality_summary": sample_quality_summary,
        "path_profile": path_profile,
        "reentry_reference": reentry,
        "symbol_regime_probability": {
            "regime": target_regime,
            "windows": probability.get("windows", {}),
            "path_profile": path_profile,
            "reentry_reference": reentry,
        },
        "brief": "",
        "guardrails": [
            "Symbol regime probability is evidence only and must not directly override Judge or Risk Gate.",
            "The market regime label comes from the index proxy; forward returns are measured on the stock itself.",
            "low_confidence=true means the probability layer cannot be a primary decision reason.",
        ],
    }
    payload["brief"] = format_regime_probability_brief(payload)
    errors = [item for item in [market_err, symbol_err] if item]
    if errors:
        payload["data_errors"] = errors
    return payload


get_regime_forward_probability_tool = ToolDefinition(
    name="get_regime_forward_probability",
    description=(
        "Compute historical forward-return probabilities for the current China A-share market regime. "
        "Use this as evidence for reentry/downside reference, not as a direct buy/sell signal."
    ),
    parameters=[
        ToolParameter(
            name="market",
            type="string",
            description="Market code. Currently only 'cn' is supported.",
            required=False,
            default="cn",
            enum=["cn"],
        ),
        ToolParameter(
            name="index_code",
            type="string",
            description="A-share index proxy, default CSI 300 '000300'.",
            required=False,
            default="000300",
        ),
        ToolParameter(
            name="regime",
            type="string",
            description="Optional target regime. If omitted, the tool detects the latest regime from history.",
            required=False,
        ),
        ToolParameter(
            name="current_price",
            type="number",
            description="Optional current index price used to compute reentry_price. Defaults to latest close.",
            required=False,
        ),
        ToolParameter(
            name="lookback_days",
            type="integer",
            description="Trading days for historical sampling, default 900, capped at 1600.",
            required=False,
            default=900,
        ),
        ToolParameter(
            name="windows",
            type="array",
            description="Forward windows in trading days, default [7,30,60,90].",
            required=False,
            default=[7, 30, 60, 90],
        ),
    ],
    handler=_handle_get_regime_forward_probability,
    category="market",
)


get_symbol_regime_probability_tool = ToolDefinition(
    name="get_symbol_regime_probability",
    description=(
        "Compute a single stock's historical forward-return probabilities conditional on the current "
        "China A-share market regime. Only use for deep-dive symbols or current holdings; evidence only."
    ),
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="A-share stock code, e.g. 600519 or 600519.SH.",
            required=True,
        ),
        ToolParameter(
            name="market",
            type="string",
            description="Market code. Currently only 'cn' is supported.",
            required=False,
            default="cn",
            enum=["cn"],
        ),
        ToolParameter(
            name="index_code",
            type="string",
            description="A-share index proxy for market regime labels, default CSI 300 '000300'.",
            required=False,
            default="000300",
        ),
        ToolParameter(
            name="regime",
            type="string",
            description="Optional target market regime. If omitted, the tool detects the latest regime from index history.",
            required=False,
        ),
        ToolParameter(
            name="current_price",
            type="number",
            description="Optional current stock price used to compute reentry_price. Defaults to latest stock close.",
            required=False,
        ),
        ToolParameter(
            name="lookback_days",
            type="integer",
            description="Trading days for historical sampling, default 900, capped at 1600.",
            required=False,
            default=900,
        ),
        ToolParameter(
            name="windows",
            type="array",
            description="Forward windows in trading days, default [7,30,60,90].",
            required=False,
            default=[7, 30, 60, 90],
        ),
    ],
    handler=_handle_get_symbol_regime_probability,
    category="market",
)


# ============================================================
# discover_watchlist_candidates
# ============================================================

def _handle_discover_watchlist_candidates(
    market: str = "cn",
    seed_symbols: Optional[List[str]] = None,
    sector_names: Optional[List[str]] = None,
    candidate_source: str = "auto",
    strategy_names: Optional[List[str]] = None,
    limit: int = 8,
) -> dict:
    """Build a deterministic candidate list for watchlist_scan."""
    effective_limit = max(1, min(int(limit or 8), 20))
    source_mode = str(candidate_source or "auto").strip().lower()
    if source_mode not in {"auto", "alphasift", "sequoia", "sector", "event_impact", "news_momentum", "news_sentiment", "fallback"}:
        source_mode = "auto"
    discovery_steps: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []

    for symbol in seed_symbols or []:
        symbol_text = str(symbol or "").strip()
        if symbol_text:
            candidates.append({
                "code": symbol_text,
                "name": symbol_text,
                "source": "user_seed",
                "reason": "用户或上下文提供的候选标的。",
            })
    if candidates:
        return _candidate_discovery_response(
            status="ok",
            market=market,
            candidates=_dedupe_candidates(candidates, effective_limit),
            discovery_steps=[{"source": "user_seed", "status": "ok", "count": len(candidates)}],
            fallback_used=False,
            candidate_source="user_seed",
            note="后续必须对候选标的逐只调用行情、技术、消息和资金工具后才能排序。",
        )

    if market != "cn":
        return {
            "status": "not_supported",
            "market": market,
            "error": "Automatic candidate discovery currently supports cn A-shares only.",
            "candidates": [],
            "next_required_tools": [],
        }

    if source_mode == "auto":
        orchestrator = CandidateExpertOrchestrator(
            timeout_s=_get_agent_timeout_attr("agent_candidate_expert_timeout_seconds", 60.0),
            max_candidates_to_deep_dive=effective_limit,
        )
        expert_result = orchestrator.discover(
            market=market,
            sector_names=sector_names,
            strategy_names=strategy_names,
            limit=effective_limit,
            tools={
                "top_sector_names": _top_sector_names,
                "fetch_sector_constituents": _fetch_sector_constituents,
                "discover_news_momentum": lambda limit, seed_candidates=None: _discover_news_momentum_candidates(
                    limit=limit,
                    seed_candidates=seed_candidates,
                ),
                "discover_event_impact": lambda market, limit: _discover_event_impact_candidates(
                    market=market,
                    limit=limit,
                ),
                "tushare_moneyflow_ind_ths": lambda limit: _data_tool_result("get_tushare_moneyflow_ind_ths", limit=limit),
                "tushare_moneyflow_ind_dc": lambda limit: _data_tool_result("get_tushare_moneyflow_ind_dc", limit=limit),
                "tushare_moneyflow_cnt_ths": lambda limit: _data_tool_result("get_tushare_moneyflow_cnt_ths", limit=limit),
                "tushare_ths_member": lambda ts_code, limit: _data_tool_result("get_tushare_ths_member", ts_code=ts_code, limit=limit),
                "tushare_moneyflow_dc": lambda limit: _data_tool_result("get_tushare_moneyflow_dc", limit=limit),
                "tushare_dragon_tiger_list": lambda limit: _data_tool_result("get_tushare_dragon_tiger_list", limit=limit),
                "tushare_dragon_tiger_inst": lambda limit: _data_tool_result("get_tushare_dragon_tiger_inst", limit=limit),
                "tushare_limit_list_ths": lambda limit: _data_tool_result("get_tushare_limit_list_ths", limit=limit),
                "tushare_limit_list_d": lambda limit: _data_tool_result("get_tushare_limit_list_d", limit=limit),
                "tushare_limit_step": lambda limit: _data_tool_result("get_tushare_limit_step", limit=limit),
                "tushare_hot_rank": lambda limit: _data_tool_result("get_tushare_hot_rank", limit=limit),
                "tushare_announcements": lambda limit: _data_tool_result("get_tushare_announcements", limit=limit),
                "tushare_stock_alerts": lambda limit: _data_tool_result("get_tushare_stock_alerts", limit=limit),
                "tushare_stock_shock": lambda limit: _data_tool_result("get_tushare_stock_shock", limit=limit),
                "tushare_share_float": lambda limit: _data_tool_result("get_tushare_share_float", limit=limit),
                "tushare_holder_trade": lambda limit: _data_tool_result("get_tushare_holder_trade", limit=limit),
                "tushare_repurchase": lambda limit: _data_tool_result("get_tushare_repurchase", limit=limit),
                "tushare_daily_basic": lambda limit: _data_tool_result("get_tushare_daily_basic", limit=limit),
                "stockapi_limit_up_pool": lambda limit: _stockapi_tool_result("get_stockapi_limit_up_pool", limit=limit),
                "stockapi_popularity_rank": lambda limit: _stockapi_tool_result("get_stockapi_popularity_rank", limit=limit),
                "stockapi_hot_money_activity": lambda limit: _stockapi_tool_result("get_stockapi_hot_money_activity", limit=limit),
                "fallback": lambda limit: _dedupe_candidates(
                    [{**item, "source": "fallback_seed_pool"} for item in DEFAULT_WATCHLIST_SEEDS],
                    limit,
                ),
            },
        )
        return _candidate_discovery_response(
            status=expert_result.get("status", "partial"),
            market=market,
            candidates=expert_result.get("candidates") or [],
            discovery_steps=expert_result.get("discovery_steps") or [],
            fallback_used=bool(expert_result.get("fallback_used")),
            candidate_source=str(expert_result.get("candidate_source") or "expert_graph_discovery"),
            note="多专家候选召回结果：各候选专家独立 discover 后合并，不代表最终推荐；后续仍需逐只深度取证、反证审查和 Judge 裁决。",
            expert_packets=expert_result.get("expert_packets") or [],
            themes=expert_result.get("themes") or [],
            capacity=expert_result.get("capacity") or {},
            quality=expert_result.get("quality") or {},
            hard_exclusion=expert_result.get("hard_exclusion") or {},
        )

    alphasift_result: Dict[str, Any] = {}
    if source_mode in {"auto", "alphasift"}:
        alphasift_result = AlphaSiftCandidateProvider().discover(
            limit=effective_limit if source_mode == "alphasift" else min(50, effective_limit * 3),
            strategy_names=strategy_names,
        )
        if source_mode == "auto" and alphasift_result.get("status") == "empty" and strategy_names:
            fallback_result = AlphaSiftCandidateProvider().discover(
                limit=min(50, effective_limit * 3),
                strategy_names=None,
            )
            if fallback_result.get("candidates"):
                fallback_result.setdefault("diagnostics", [])
                fallback_result["diagnostics"] = [
                    {
                        "source": "alphasift_strategy_filter",
                        "status": "fallback_to_all",
                        "requested": strategy_names,
                        "reason": "Requested strategy names did not match AlphaSift YAML strategies in auto mode.",
                    },
                    *(fallback_result.get("diagnostics") or []),
                ]
                alphasift_result = fallback_result
        alphasift_candidates = alphasift_result.get("candidates") or []
        discovery_steps.append({
            "source": "alphasift",
            "status": alphasift_result.get("status", "failed"),
            "count": len(alphasift_candidates),
            "db_path": alphasift_result.get("db_path"),
            "strategies_dir": alphasift_result.get("strategies_dir"),
            "strategy_names": alphasift_result.get("strategy_names", []),
            "diagnostics": alphasift_result.get("diagnostics", []),
            **({"error": alphasift_result.get("error")} if alphasift_result.get("error") else {}),
        })
        candidates.extend(alphasift_candidates)
        if source_mode == "alphasift":
            candidates = _dedupe_candidates(candidates, effective_limit)
            return _candidate_discovery_response(
                status="ok" if candidates else "partial",
                market=market,
                candidates=candidates,
                discovery_steps=discovery_steps,
                fallback_used=False,
                candidate_source="alphasift",
                note="AlphaSift YAML 策略只生成候选池，不代表最终推荐；后续必须逐只取证后再排序和配置仓位。",
            )

    sequoia_result: Dict[str, Any] = {}
    if source_mode in {"auto", "sequoia"}:
        sequoia_result = SequoiaCandidateProvider().discover(
            limit=effective_limit if source_mode == "sequoia" else min(50, effective_limit * 3),
            strategy_names=strategy_names,
        )
        sequoia_candidates = sequoia_result.get("candidates") or []
        discovery_steps.append({
            "source": "sequoia",
            "status": sequoia_result.get("status", "failed"),
            "count": len(sequoia_candidates),
            "db_path": sequoia_result.get("db_path"),
            "strategy_names": sequoia_result.get("strategy_names", []),
            "diagnostics": sequoia_result.get("diagnostics", []),
            **({"error": sequoia_result.get("error")} if sequoia_result.get("error") else {}),
        })
        candidates.extend(sequoia_candidates)
        if source_mode == "sequoia":
            candidates = _dedupe_candidates(candidates, effective_limit)
            return _candidate_discovery_response(
                status="ok" if candidates else "partial",
                market=market,
                candidates=candidates,
                discovery_steps=discovery_steps,
                fallback_used=False,
                candidate_source="sequoia",
                note="Sequoia 量化策略只生成候选池，不代表最终推荐；后续必须逐只取证后再排序和配置仓位。",
            )

    event_result: Dict[str, Any] = {}
    if source_mode in {"auto", "event_impact", "news_sentiment"}:
        event_result = _discover_event_impact_candidates(
            market=market,
            limit=effective_limit if source_mode in {"event_impact", "news_sentiment"} else min(20, effective_limit * 2),
        )
        event_candidates = event_result.get("candidates") or []
        discovery_steps.append({
            "source": "event_impact",
            "status": event_result.get("status", "failed"),
            "count": len(event_candidates),
            "events": event_result.get("events", []),
            "queries": event_result.get("queries", []),
            "diagnostics": event_result.get("diagnostics", []),
        })
        candidates.extend(event_candidates)
        if source_mode in {"event_impact", "news_sentiment"}:
            candidates = _dedupe_candidates(candidates, effective_limit)
            return _candidate_discovery_response(
                status="ok" if candidates else "partial",
                market=market,
                candidates=candidates,
                discovery_steps=discovery_steps,
                fallback_used=False,
                candidate_source="event_impact",
                note="事件影响链先形成事件/主题观察，只有 7 日窗口内出现后续验证事实才生成个股候选；未验证事件不得直接推导个股。",
            )

    news_momentum_result: Dict[str, Any] = {}
    if source_mode in {"auto", "news_momentum"}:
        news_momentum_result = _discover_news_momentum_candidates(
            limit=effective_limit if source_mode == "news_momentum" else min(20, effective_limit * 2),
            seed_candidates=candidates,
        )
        news_momentum_candidates = news_momentum_result.get("candidates") or []
        discovery_steps.append({
            "source": "news_momentum",
            "status": news_momentum_result.get("status", "failed"),
            "count": len(news_momentum_candidates),
            "queries": news_momentum_result.get("queries", []),
            "diagnostics": news_momentum_result.get("diagnostics", []),
        })
        candidates.extend(news_momentum_candidates)
        if source_mode == "news_momentum":
            candidates = _dedupe_candidates(candidates, effective_limit)
            return _candidate_discovery_response(
                status="ok" if candidates else "partial",
                market=market,
                candidates=candidates,
                discovery_steps=discovery_steps,
                fallback_used=False,
                candidate_source="news_momentum",
                note="消息面候选只接收公司级新闻/公告硬事件；减持、监管处罚、问询等负面事件只作为风险提示，不作为买入候选。",
            )

    if source_mode == "fallback":
        candidates = [
            {**item, "source": "fallback_seed_pool"}
            for item in DEFAULT_WATCHLIST_SEEDS
        ]
        discovery_steps.append({"source": "fallback_seed_pool", "status": "ok", "count": len(candidates)})
        candidates = _dedupe_candidates(candidates, effective_limit)
        return _candidate_discovery_response(
            status="partial",
            market=market,
            candidates=candidates,
            discovery_steps=discovery_steps,
            fallback_used=True,
            candidate_source="fallback",
            note="使用固定候选种子池，仅用于保证后续取证链路可运行，不代表推荐。",
        )

    sectors = [str(name).strip() for name in (sector_names or []) if str(name or "").strip()]
    if source_mode in {"auto", "sector"} and not sectors:
        sectors = _top_sector_names(top_n=5)
        discovery_steps.append({"source": "get_sector_rankings", "status": "ok" if sectors else "empty", "sectors": sectors})

    for sector in sectors if source_mode in {"auto", "sector"} else []:
        sector_result = _fetch_sector_constituents(
            sector,
            limit=effective_limit if source_mode == "sector" else min(20, effective_limit * 2),
            include_diagnostics=True,
        )
        if isinstance(sector_result, tuple):
            sector_candidates, sector_diagnostics = sector_result
        else:
            sector_candidates, sector_diagnostics = sector_result, []
        discovery_steps.append({
            "source": "sector_constituents",
            "sector": sector,
            "status": "ok" if sector_candidates else "empty",
            "count": len(sector_candidates),
            "diagnostics": sector_diagnostics,
        })
        candidates.extend(sector_candidates)
        if source_mode == "sector" and len(_dedupe_candidates(candidates, effective_limit)) >= effective_limit:
            break

    sector_local_fallback_used = False
    if source_mode == "sector" and not candidates:
        local_candidates, local_steps = _discover_local_strategy_candidates_for_sector_fallback(
            effective_limit=effective_limit,
            strategy_names=strategy_names,
            reason="sector_constituents_empty_or_timeout",
        )
        discovery_steps.extend(local_steps)
        if local_candidates:
            sector_local_fallback_used = True
            candidates.extend(local_candidates)

    fallback_used = False
    if not candidates:
        fallback_used = True
        candidates = [
            {**item, "source": "fallback_seed_pool"}
            for item in DEFAULT_WATCHLIST_SEEDS
        ]
        discovery_steps.append({"source": "fallback_seed_pool", "status": "ok", "count": len(candidates)})

    candidates = _dedupe_candidates(candidates, effective_limit)
    candidate_source = (
        "fallback"
        if fallback_used
        else ("sector_local_fallback" if sector_local_fallback_used else ("multi_recall" if source_mode == "auto" else "sector"))
    )
    return _candidate_discovery_response(
        status="partial" if fallback_used else "ok",
        market=market,
        candidates=candidates,
        discovery_steps=discovery_steps,
        fallback_used=fallback_used,
        candidate_source=candidate_source,
        note="这是多路召回后的候选发现结果，不是最终推荐。必须继续对候选逐只取证后才能输出排序和仓位配置。",
    )


def _candidate_discovery_response(
    *,
    status: str,
    market: str,
    candidates: List[Dict[str, Any]],
    discovery_steps: List[Dict[str, Any]],
    fallback_used: bool,
    candidate_source: str,
    note: str,
    expert_packets: Optional[List[Dict[str, Any]]] = None,
    themes: Optional[List[Dict[str, Any]]] = None,
    capacity: Optional[Dict[str, Any]] = None,
    quality: Optional[Dict[str, Any]] = None,
    hard_exclusion: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if hard_exclusion is None:
        candidates, hard_exclusion = _apply_candidate_hard_exclusion_for_response(candidates)
        discovery_steps = [
            *discovery_steps,
            {
                "source": "candidate_hard_exclusion",
                "status": "ok",
                "count": hard_exclusion.get("excluded_count", 0),
                "diagnostics": hard_exclusion,
            },
        ]
    if quality is None:
        quality = _build_candidate_quality_summary(candidates, hard_exclusion)
    payload = {
        "status": status,
        "market": market,
        "candidate_source": candidate_source,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "discovery_steps": discovery_steps,
        "fallback_used": fallback_used,
        "next_required_tools": [
            "get_realtime_quote",
            "analyze_trend",
            "calculate_ma",
            "get_volume_analysis",
            "analyze_pattern",
            "search_stock_news",
            "score_stock_news_sentiment",
            "get_capital_flow",
        ],
        "note": note,
        "quality": quality,
        "hard_exclusion": hard_exclusion,
    }
    if expert_packets is not None:
        payload["expert_packets"] = expert_packets
    if themes is not None:
        payload["themes"] = themes
    if capacity is not None:
        payload["capacity"] = capacity
    try:
        from src.agent.candidate_pool_store import CandidatePoolStore

        saved = CandidatePoolStore().save_run(payload)
        payload["candidate_pool_run_id"] = saved.get("run_id")
        payload["candidate_pool_persisted"] = True
    except Exception as exc:
        logger.warning("Candidate pool persistence skipped: %s", exc)
        payload["candidate_pool_persisted"] = False
        payload["candidate_pool_persist_error"] = str(exc)
    return _sanitize_non_finite_numbers(payload)


def _sanitize_non_finite_numbers(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _sanitize_non_finite_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_non_finite_numbers(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_non_finite_numbers(item) for item in value]
    return value


discover_watchlist_candidates_tool = ToolDefinition(
    name="discover_watchlist_candidates",
    description=(
        "Discover seed stock candidates for watchlist_scan / stock selection. "
        "Use this before single-stock quote/technical/news/capital tools when the user asks to pick stocks "
        "but did not provide stock codes. Returns candidate stock codes and required next tools."
    ),
    parameters=[
        ToolParameter(
            name="market",
            type="string",
            description="Market to discover candidates from. Currently 'cn' A-shares are supported.",
            required=False,
            default="cn",
            enum=["cn", "hk", "us"],
        ),
        ToolParameter(
            name="seed_symbols",
            type="array",
            description="Optional user/context-provided stock codes to use directly as candidates.",
            required=False,
            default=[],
        ),
        ToolParameter(
            name="sector_names",
            type="array",
            description="Optional sector names from get_sector_rankings to fetch constituents from.",
            required=False,
            default=[],
        ),
        ToolParameter(
            name="candidate_source",
            type="string",
            description="Candidate source: auto, alphasift, sequoia, sector, event_impact, news_momentum, news_sentiment, or fallback. auto tries AlphaSift YAML candidates, Sequoia quantitative candidates, event-impact candidates, company-news momentum candidates, sector constituents, and fallback seeds. news_sentiment is kept as a compatibility alias for event_impact.",
            required=False,
            default="auto",
            enum=["auto", "alphasift", "sequoia", "sector", "event_impact", "news_momentum", "news_sentiment", "fallback"],
        ),
        ToolParameter(
            name="strategy_names",
            type="array",
            description="Optional strategy names. AlphaSift supports volume_breakout, capital_heat, balanced_alpha, quality_value, dual_low, momentum_quality, oversold_reversal, shrink_pullback. Sequoia supports ma_volume, turtle_trade, high_tight_flag, limit_up_shakeout, uptrend_limit_down, rps_breakout, or all.",
            required=False,
            default=[],
        ),
        ToolParameter(
            name="limit",
            type="integer",
            description="Maximum number of candidates to return, 1-20 (default: 8).",
            required=False,
            default=8,
        ),
    ],
    handler=_handle_discover_watchlist_candidates,
    category="market",
)


ALL_MARKET_TOOLS = [
    detect_market_regime_tool,
    get_regime_forward_probability_tool,
    get_symbol_regime_probability_tool,
    get_market_indices_tool,
    get_sector_rankings_tool,
    discover_watchlist_candidates_tool,
]
