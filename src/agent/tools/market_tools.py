# -*- coding: utf-8 -*-
"""
Market tools — wraps DataFetcherManager market-level methods as agent tools.

Tools:
- get_market_indices: major market index data
- get_sector_rankings: sector performance rankings
- discover_watchlist_candidates: seed stock candidates for stock selection
"""

import logging
from typing import Any, Dict, Iterable, List, Optional

from src.agent.candidate_providers.sequoia_provider import SequoiaCandidateProvider
from src.agent.tools.registry import ToolParameter, ToolDefinition

logger = logging.getLogger(__name__)


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
            payload["name"] = str(payload.get("name") or payload.get("stock_name") or code)
            payload["recall_sources"] = [source]
            payload["raw_recall_count"] = 1
            payload["signal_score"] = round(base_score, 2)
            by_code[code] = payload
            continue

        current = by_code[code]
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
    merged.sort(
        key=lambda item: (
            float(item.get("signal_score") or 0),
            len(item.get("recall_sources") or []),
            len(item.get("matched_strategies") or []),
            str(item.get("code") or ""),
        ),
        reverse=True,
    )
    return merged[: max(1, limit)]


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


def _fetch_sector_constituents(sector_name: str, limit: int) -> List[Dict[str, Any]]:
    try:
        import akshare as ak
    except Exception as exc:
        logger.warning("AkShare unavailable for candidate discovery: %s", exc)
        return []

    fetchers = (
        ("industry", getattr(ak, "stock_board_industry_cons_em", None)),
        ("concept", getattr(ak, "stock_board_concept_cons_em", None)),
    )
    candidates: List[Dict[str, Any]] = []
    for source, fetcher in fetchers:
        if fetcher is None:
            continue
        try:
            df = fetcher(symbol=sector_name)
        except Exception as exc:
            logger.debug("Candidate discovery failed for sector=%s source=%s: %s", sector_name, source, exc)
            continue
        if df is None or getattr(df, "empty", True):
            continue
        for row in df.head(limit).to_dict(orient="records"):
            normalized = _normalize_stock_candidate(
                row,
                source=f"akshare:{source}:{sector_name}",
                reason=f"来自强势板块「{sector_name}」成分股。",
            )
            if normalized:
                candidates.append(normalized)
        if candidates:
            break
    return candidates


# ============================================================
# get_market_indices
# ============================================================

def _handle_get_market_indices(region: str = "cn") -> dict:
    """Get major market indices."""
    manager = _get_fetcher_manager()
    indices = manager.get_main_indices(region=region)

    if not indices:
        return {"error": f"No market index data available for region '{region}'"}

    return {
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
    manager = _get_fetcher_manager()
    result = manager.get_sector_rankings(n=top_n)

    if result is None:
        return {"error": "No sector ranking data available"}

    # get_sector_rankings returns Tuple[List[Dict], List[Dict]]
    # (top_sectors, bottom_sectors)
    if isinstance(result, tuple) and len(result) == 2:
        top_sectors, bottom_sectors = result
        return {
            "top_sectors": top_sectors,
            "bottom_sectors": bottom_sectors,
        }
    elif isinstance(result, list):
        return {"sectors": result}
    else:
        return {"data": str(result)}


get_sector_rankings_tool = ToolDefinition(
    name="get_sector_rankings",
    description="Get sector/industry performance rankings. Returns top N and bottom N "
                "sectors by daily change percentage. Useful for sector rotation analysis.",
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
    if source_mode not in {"auto", "sequoia", "sector", "fallback"}:
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
        return {
            "status": "ok",
            "market": market,
            "candidate_count": len(_dedupe_candidates(candidates, effective_limit)),
            "candidates": _dedupe_candidates(candidates, effective_limit),
            "discovery_steps": [{"source": "user_seed", "status": "ok"}],
            "next_required_tools": [
                "get_realtime_quote",
                "analyze_trend",
                "calculate_ma",
                "get_volume_analysis",
                "analyze_pattern",
                "search_stock_news",
                "get_capital_flow",
            ],
            "note": "后续必须对候选标的逐只调用行情、技术、消息和资金工具后才能排序。",
        }

    if market != "cn":
        return {
            "status": "not_supported",
            "market": market,
            "error": "Automatic candidate discovery currently supports cn A-shares only.",
            "candidates": [],
            "next_required_tools": [],
        }

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
        sector_candidates = _fetch_sector_constituents(sector, limit=effective_limit if source_mode == "sector" else min(20, effective_limit * 2))
        discovery_steps.append({
            "source": "sector_constituents",
            "sector": sector,
            "status": "ok" if sector_candidates else "empty",
            "count": len(sector_candidates),
        })
        candidates.extend(sector_candidates)
        if source_mode == "sector" and len(_dedupe_candidates(candidates, effective_limit)) >= effective_limit:
            break

    fallback_used = False
    if not candidates:
        fallback_used = True
        candidates = [
            {**item, "source": "fallback_seed_pool"}
            for item in DEFAULT_WATCHLIST_SEEDS
        ]
        discovery_steps.append({"source": "fallback_seed_pool", "status": "ok", "count": len(candidates)})

    candidates = _dedupe_candidates(candidates, effective_limit)
    candidate_source = "fallback" if fallback_used else ("multi_recall" if source_mode == "auto" else "sector")
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
) -> Dict[str, Any]:
    return {
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
            "get_capital_flow",
        ],
        "note": note,
    }


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
            description="Candidate source: auto, sequoia, sector, or fallback. auto tries Sequoia quantitative candidates first, then sector constituents, then fallback seeds.",
            required=False,
            default="auto",
            enum=["auto", "sequoia", "sector", "fallback"],
        ),
        ToolParameter(
            name="strategy_names",
            type="array",
            description="Optional Sequoia strategy names: ma_volume, turtle_trade, high_tight_flag, limit_up_shakeout, uptrend_limit_down, rps_breakout, or all.",
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
    get_market_indices_tool,
    get_sector_rankings_tool,
    discover_watchlist_candidates_tool,
]
