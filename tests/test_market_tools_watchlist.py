import sys
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from src.agent.candidate_providers.alphasift_provider import AlphaSiftCandidateProvider
from src.agent.candidate_providers.sequoia_provider import SequoiaCandidateProvider
from src.agent.tools import market_tools
from src.agent.tools.market_tools import _candidate_discovery_response, _handle_discover_watchlist_candidates


def _isolated_candidate_env(tmp_path, **overrides):
    env = {
        "DATABASE_PATH": str(tmp_path / "isolated-stock-analysis.db"),
        "AGENT_FUNDAMENTAL_CANDIDATE_DB_PATH": str(tmp_path / "missing-fundamental.db"),
        "AGENT_CANDIDATE_BLACKLIST_CODES": "",
        "AGENT_CANDIDATE_MIN_AVG_AMOUNT": "0",
        "AGENT_CANDIDATE_MIN_LISTING_DAYS": "0",
        "AGENT_CANDIDATE_ENFORCE_NAME_CODE_MATCH": "true",
    }
    env.update({key: str(value) for key, value in overrides.items()})
    return env


def _write_daily_db(path, rows):
    import sqlite3

    with sqlite3.connect(path) as conn:
        pd.DataFrame(rows).to_sql("stock_daily", conn, if_exists="replace", index=False)


def _write_alphasift_strategy_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "unit_breakout.yaml").write_text(
        "\n".join(
            [
                "name: unit_breakout",
                "display_name: Unit Breakout",
                "description: Unit test AlphaSift-style breakout strategy.",
                "category: trend",
                "tags:",
                "  - unit",
                "  - breakout",
                "screening:",
                "  enabled: true",
                "  market_scope: [cn]",
                "  hard_filters:",
                "    exclude_st: true",
                "    amount_min: 1000000",
                "    change_pct_min: 0.1",
                "  factor_weights:",
                "    momentum: 0.5",
                "    activity: 0.3",
                "    liquidity: 0.2",
                "  ranking_hints: Prefer liquid breakouts with confirmed follow-up evidence.",
                "  max_output: 5",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_profiled_alphasift_strategy_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "profiled_heat.yaml").write_text(
        "\n".join(
            [
                "name: profiled_heat",
                "display_name: Profiled Heat",
                "description: Strategy with AlphaSift scoring/risk profile fields.",
                "category: momentum",
                "tags: [capital_flow]",
                "screening:",
                "  enabled: true",
                "  market_scope: [cn]",
                "  hard_filters:",
                "    exclude_st: true",
                "    amount_min: 1000000",
                "    price_min: 1",
                "  factor_weights:",
                "    momentum: 0.6",
                "    activity: 0.4",
                "  scoring_profile:",
                "    momentum_chase_start_pct: 3.0",
                "    momentum_chase_penalty_slope: 15.0",
                "    activity_ideal_volume_ratio: 2.0",
                "    activity_high_volume_ratio: 4.0",
                "    activity_ideal_turnover_rate: 3.0",
                "    activity_high_turnover_rate: 10.0",
                "  risk_profile:",
                "    chase_change_pct: 4.0",
                "    abnormal_volume_ratio: 5.0",
                "    high_turnover_rate: 12.0",
                "    weak_signal_score: 45.0",
                "  max_output: 5",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _bars_for_turtle(symbol: str):
    rows = []
    base = pd.Timestamp("2026-01-01")
    for i in range(22):
        close = 10.0 + i * 0.1
        high = close + 0.2
        open_price = close - 0.05
        if i == 21:
            close = 14.0
            high = 14.2
            open_price = 13.0
        rows.append({
            "symbol": symbol,
            "date": (base + pd.Timedelta(days=i)).strftime("%Y-%m-%d"),
            "open": open_price,
            "high": high,
            "low": close - 0.3,
            "close": close,
            "volume": 1_000_000 + i,
            "turnover": 150_000_000 if i == 21 else 20_000_000,
        })
    return rows


def _bars_for_hot_chase(symbol: str):
    rows = []
    base = pd.Timestamp("2026-01-01")
    for i in range(80):
        close = 10.0 + i * 0.02
        volume = 900_000
        turnover = 80_000_000
        if i == 79:
            close = 14.8
            volume = 18_000_000
            turnover = 1_800_000_000
        rows.append({
            "symbol": symbol,
            "date": (base + pd.Timedelta(days=i)).strftime("%Y-%m-%d"),
            "open": close * 0.9 if i == 79 else close - 0.05,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": volume,
            "turnover": turnover,
        })
    return rows


def _bars_for_alphasift_breakout(symbol: str):
    rows = []
    base = pd.Timestamp("2026-01-01")
    for i in range(80):
        close = 10.0 + i * 0.03
        high = close + 0.2
        low = close - 0.2
        open_price = close - 0.05
        volume = 900_000
        turnover = 120_000_000
        if i >= 60:
            close = 12.0 + (i - 60) * 0.02
            high = close + 0.25
            low = close - 0.25
        if i == 79:
            open_price = 12.0
            close = 13.6
            high = 13.9
            low = 11.9
            volume = 3_200_000
            turnover = 360_000_000
        rows.append({
            "symbol": symbol,
            "date": (base + pd.Timedelta(days=i)).strftime("%Y-%m-%d"),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "turnover": turnover,
        })
    return rows


def test_discover_watchlist_candidates_uses_seed_symbols_directly():
    result = _handle_discover_watchlist_candidates(
        seed_symbols=["600519", "600519", "300750"],
        limit=5,
    )

    assert result["status"] == "ok"
    assert [item["code"] for item in result["candidates"]] == ["600519", "300750"]
    assert "get_realtime_quote" in result["next_required_tools"]


def test_discover_watchlist_candidates_falls_back_when_sector_lookup_empty(tmp_path):
    with patch.dict("os.environ", _isolated_candidate_env(tmp_path, SEQUOIA_CANDIDATE_DB_PATH="/tmp/not-exists-sequoia.db", ALPHASIFT_CANDIDATE_DB_PATH="/tmp/not-exists-alphasift.db")), patch(
        "src.agent.tools.market_tools._top_sector_names",
        return_value=[],
    ):
        result = _handle_discover_watchlist_candidates(limit=3)

    assert result["status"] == "partial"
    assert result["fallback_used"] is True
    assert len(result["candidates"]) == 3
    assert result["candidates"][0]["source"] == "fallback_seed_pool"
    assert result["candidates"][0]["reason_dimensions"][0]["label"] == "策略"


def test_discover_watchlist_candidates_event_impact_watch_only_does_not_pick_stocks():
    class DummySearchResult:
        def __init__(self, title, snippet, url, source, published_date):
            self.title = title
            self.snippet = snippet
            self.url = url
            self.source = source
            self.published_date = published_date

    class DummySearchResponse:
        def __init__(self, query, provider, success, results):
            self.query = query
            self.provider = provider
            self.success = success
            self.results = results
            self.error_message = None

    class DummySearchService:
        is_available = True

        def search_general_news(self, query, max_results=5, days=1):
            if days > 1:
                return DummySearchResponse(
                    query=query,
                    provider="Tavily",
                    success=True,
                    results=[
                        DummySearchResult(
                            title="市场继续观察相关地缘事件",
                            snippet="暂无油价、运价、保险费、板块异动或资金流入等后续验证事实，油价变化仍待验证。",
                            url="https://example.com/news/watch",
                            source="example.com",
                            published_date="2026-05-13",
                        )
                    ],
                )
            return DummySearchResponse(
                query=query,
                provider="Tavily",
                success=True,
                results=[
                    DummySearchResult(
                        title="霍尔木兹海峡允许通行，原油风险溢价回落",
                        snippet="事件仍处突发阶段，后续油价、运价和保险费变化仍待验证。",
                        url="https://example.com/news/hormuz",
                        source="example.com",
                        published_date="2026-05-13",
                    ),
                ],
            )

    with patch("src.agent.tools.market_tools._build_search_service_for_candidates", return_value=DummySearchService()), patch(
        "src.agent.tools.market_tools._ingest_event_watch_to_graphiti",
    ):
        result = _handle_discover_watchlist_candidates(
            candidate_source="event_impact",
            limit=3,
        )

    assert result["status"] == "partial"
    assert result["candidate_source"] == "event_impact"
    assert result["candidate_count"] == 0
    assert result["discovery_steps"][0]["source"] == "event_impact"
    assert result["discovery_steps"][0]["events"]
    assert result["discovery_steps"][0]["events"][0]["maturity"] in {"breaking", "developing"}
    assert all(
        match["status"] == "watch_only"
        for match in result["discovery_steps"][0]["events"][0]["validation_matches"]
    )


def test_discover_watchlist_candidates_event_impact_uses_validated_theme(tmp_path):
    class DummySearchResult:
        def __init__(self, title, snippet, url, source, published_date):
            self.title = title
            self.snippet = snippet
            self.url = url
            self.source = source
            self.published_date = published_date

    class DummySearchResponse:
        def __init__(self, query, provider, success, results):
            self.query = query
            self.provider = provider
            self.success = success
            self.results = results
            self.error_message = None

    class DummySearchService:
        is_available = True

        def search_general_news(self, query, max_results=5, days=1):
            if days <= 1:
                return DummySearchResponse(
                    query=query,
                    provider="Tavily",
                    success=True,
                    results=[
                        DummySearchResult(
                            title="AI芯片出口限制出现缓和信号，半导体产业链关注度提升",
                            snippet="事件可能影响科技政策风险，但个股影响仍需后续事实验证。",
                            url="https://example.com/news/ai-chip",
                            source="example.com",
                            published_date="2026-05-13",
                        )
                    ],
                )
            return DummySearchResponse(
                query=query,
                provider="Tavily",
                success=True,
                results=[
                    DummySearchResult(
                        title="半导体板块异动并出现资金流入，设备订单预期改善",
                        snippet="板块异动、资金流入、订单等后续事实被市场关注。",
                        url="https://example.com/news/semiconductor-followup",
                        source="example.com",
                        published_date="2026-05-13",
                    )
                ],
            )

    with patch("src.agent.tools.market_tools._build_search_service_for_candidates", return_value=DummySearchService()), patch(
        "src.agent.tools.market_tools._ingest_event_watch_to_graphiti",
    ), patch(
        "src.agent.tools.market_tools._fetch_sector_constituents",
        return_value=[
            {
                "code": "688981",
                "name": "中芯国际",
                "source": "akshare:industry:半导体",
                "reason": "半导体板块成分股。",
            }
        ],
    ):
        result = _handle_discover_watchlist_candidates(
            candidate_source="event_impact",
            limit=3,
        )

    assert result["status"] == "ok"
    assert result["candidate_source"] == "event_impact"
    assert result["candidates"][0]["source"] == "event_impact:validated_theme"
    assert result["candidates"][0]["validated_theme"] in {"人工智能", "半导体", "算力", "机器人", "工业自动化"}
    assert any(item["label"] == "情绪/事件" for item in result["candidates"][0]["reason_dimensions"])


def test_discover_watchlist_candidates_news_momentum_company_event():
    class DummySearchResult:
        def __init__(self, title, snippet, url, source, published_date):
            self.title = title
            self.snippet = snippet
            self.url = url
            self.source = source
            self.published_date = published_date

    class DummySearchResponse:
        def __init__(self, query, provider, success, results):
            self.query = query
            self.provider = provider
            self.success = success
            self.results = results
            self.error_message = None

    class DummySearchService:
        is_available = True

        def search_general_news(self, query, max_results=6, days=3):
            return DummySearchResponse(
                query=query,
                provider="Tavily",
                success=True,
                results=[
                    DummySearchResult(
                        title="测试股份签订重大合同并获得大订单",
                        snippet="公司公告称订单增长将改善收入可见度。",
                        url="https://example.com/news/order",
                        source="example.com",
                        published_date="2026-05-14",
                    )
                ],
            )

        def search_stock_news(self, stock_code, stock_name, max_results=5, focus_keywords=None):
            return DummySearchResponse(
                query=" ".join(focus_keywords or []),
                provider="Tavily",
                success=True,
                results=[],
            )

    with patch("src.agent.tools.market_tools._build_search_service_for_candidates", return_value=DummySearchService()), patch(
        "src.agent.tools.market_tools._iter_candidate_name_pairs",
        return_value=[("600001", "测试股份")],
    ):
        result = _handle_discover_watchlist_candidates(
            candidate_source="news_momentum",
            limit=3,
        )

    assert result["status"] == "ok"
    assert result["candidate_source"] == "news_momentum"
    assert result["candidates"][0]["code"] == "600001"
    assert result["candidates"][0]["source"] == "news_momentum:company_event"
    assert any(item["label"] == "消息面" for item in result["candidates"][0]["reason_dimensions"])
    assert result["candidates"][0]["metrics"]["positive_news_events"] >= 1


def test_sequoia_provider_returns_structured_strategy_candidates(tmp_path):
    db_path = tmp_path / "sequoia.db"
    _write_daily_db(db_path, _bars_for_turtle("600001"))

    provider = SequoiaCandidateProvider(str(db_path))
    result = provider.discover(limit=5, strategy_names=["turtle_trade"])

    assert result["status"] == "ok"
    assert result["candidate_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["code"] == "600001"
    assert candidate["source"] == "sequoia:turtle_trade"
    assert candidate["matched_strategies"] == ["turtle_trade"]
    assert "breakout" in candidate["strategy_tags"]
    assert candidate["signal_score"] > 0


def test_sequoia_provider_supports_private_placement_event_strategy_without_daily_db(tmp_path):
    df = pd.DataFrame(
        [
            {
                "股票代码": "600111.SH",
                "发行方式": "定向增发",
                "发行日期": pd.Timestamp.today().strftime("%Y-%m-%d"),
                "实际募集资金总额": 123000000,
                "增发价格": 12.34,
            },
            {
                "股票代码": "600222.SH",
                "发行方式": "公开增发",
                "发行日期": pd.Timestamp.today().strftime("%Y-%m-%d"),
            },
        ]
    )
    provider = SequoiaCandidateProvider(str(tmp_path / "missing.db"))

    with patch.dict(sys.modules, {"akshare": SimpleNamespace(stock_qbzf_em=lambda: df)}):
        result = provider.discover(limit=5, strategy_names=["private_placement"])

    assert result["status"] == "ok"
    assert result["candidate_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["code"] == "600111"
    assert candidate["source"] == "sequoia:private_placement"
    assert candidate["matched_strategies"] == ["private_placement"]
    assert "announcement" in candidate["strategy_tags"]


def test_alphasift_provider_returns_yaml_strategy_candidates(tmp_path):
    db_path = tmp_path / "alphasift.db"
    strategy_dir = tmp_path / "strategies"
    _write_daily_db(db_path, _bars_for_alphasift_breakout("600003"))
    _write_alphasift_strategy_dir(strategy_dir)

    provider = AlphaSiftCandidateProvider(str(db_path), str(strategy_dir))
    result = provider.discover(limit=5, strategy_names=["unit_breakout"])

    assert result["status"] == "ok"
    assert result["provider"] == "alphasift"
    assert result["candidate_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["code"] == "600003"
    assert any(item["label"] == "策略" for item in candidate["reason_dimensions"])
    assert any(item["label"] == "资金面" for item in candidate["reason_dimensions"])
    assert candidate["source"] == "alphasift:unit_breakout"
    assert candidate["matched_strategies"] == ["unit_breakout"]
    assert "breakout" in candidate["strategy_tags"]
    strategy_detail = next(item["detail"] for item in candidate["reason_dimensions"] if item["label"] == "策略")
    capital_detail = next(item["detail"] for item in candidate["reason_dimensions"] if item["label"] == "资金面")
    assert "流动性" not in strategy_detail
    assert "动量" not in strategy_detail
    assert capital_detail.startswith("流动性代理：")
    assert candidate["signal_score"] > 0
    assert "ranking_hints" in candidate


def test_alphasift_provider_consumes_scoring_and_risk_profiles(tmp_path):
    db_path = tmp_path / "alphasift.db"
    strategy_dir = tmp_path / "strategies"
    _write_daily_db(db_path, _bars_for_hot_chase("600006"))
    _write_profiled_alphasift_strategy_dir(strategy_dir)

    profiled = AlphaSiftCandidateProvider(str(db_path), str(strategy_dir)).discover(
        limit=5,
        strategy_names=["profiled_heat"],
    )
    assert profiled["status"] == "ok"
    score_with_profile = profiled["candidates"][0]["signal_score"]

    (strategy_dir / "profiled_heat.yaml").write_text(
        (strategy_dir / "profiled_heat.yaml")
        .read_text(encoding="utf-8")
        .replace("momentum_chase_penalty_slope: 15.0", "momentum_chase_penalty_slope: 0.0")
        .replace("activity_high_volume_ratio: 4.0", "activity_high_volume_ratio: 99.0")
        .replace("activity_high_turnover_rate: 10.0", "activity_high_turnover_rate: 99.0")
        .replace("chase_change_pct: 4.0", "chase_change_pct: 99.0")
        .replace("abnormal_volume_ratio: 5.0", "abnormal_volume_ratio: 99.0")
        .replace("high_turnover_rate: 12.0", "high_turnover_rate: 99.0"),
        encoding="utf-8",
    )
    relaxed = AlphaSiftCandidateProvider(str(db_path), str(strategy_dir)).discover(
        limit=5,
        strategy_names=["profiled_heat"],
    )

    assert relaxed["candidates"][0]["signal_score"] > score_with_profile


def test_discover_watchlist_candidates_alphasift_source(tmp_path):
    db_path = tmp_path / "alphasift.db"
    strategy_dir = tmp_path / "strategies"
    _write_daily_db(db_path, _bars_for_alphasift_breakout("600003"))
    _write_alphasift_strategy_dir(strategy_dir)

    with patch.dict("os.environ", _isolated_candidate_env(tmp_path, ALPHASIFT_CANDIDATE_DB_PATH=str(db_path), ALPHASIFT_STRATEGY_DIR=str(strategy_dir))):
        result = _handle_discover_watchlist_candidates(
            candidate_source="alphasift",
            strategy_names=["unit_breakout"],
            limit=5,
        )

    assert result["status"] == "ok"
    assert result["candidate_source"] == "alphasift"
    assert result["discovery_steps"][0]["source"] == "alphasift"
    assert result["candidates"][0]["code"] == "600003"


def test_auto_alphasift_falls_back_to_yaml_strategies_when_requested_names_are_sequoia(tmp_path):
    db_path = tmp_path / "alphasift.db"
    strategy_dir = tmp_path / "strategies"
    _write_daily_db(db_path, _bars_for_alphasift_breakout("600003"))
    _write_alphasift_strategy_dir(strategy_dir)

    with patch.dict("os.environ", _isolated_candidate_env(tmp_path, ALPHASIFT_CANDIDATE_DB_PATH=str(db_path), ALPHASIFT_STRATEGY_DIR=str(strategy_dir))), patch(
        "src.agent.tools.market_tools.SequoiaCandidateProvider.discover",
        return_value={"status": "empty", "candidate_count": 0, "candidates": [], "diagnostics": []},
    ), patch(
        "src.agent.tools.market_tools._top_sector_names",
        return_value=[],
    ), patch(
        "src.agent.tools.market_tools._discover_event_impact_candidates",
        return_value={"status": "empty", "candidates": [], "events": [], "queries": [], "diagnostics": []},
    ), patch(
        "src.agent.tools.market_tools._discover_news_momentum_candidates",
        return_value={"status": "empty", "candidates": [], "queries": [], "diagnostics": []},
    ), patch(
        "src.agent.tools.market_tools._data_tool_result",
        return_value={"status": "empty", "candidates": [], "items": [], "data": []},
    ), patch(
        "src.agent.tools.market_tools._stockapi_tool_result",
        return_value={"status": "empty", "candidates": [], "items": [], "data": []},
    ):
        result = _handle_discover_watchlist_candidates(
            candidate_source="auto",
            strategy_names=["turtle_trade", "rps_breakout"],
            limit=5,
        )

    assert result["status"] == "ok"
    assert result["candidate_source"] == "expert_graph_discovery"
    strategy_step = next(step for step in result["discovery_steps"] if step["source"] == "candidate_expert:strategy_factor_expert")
    assert strategy_step["count"] == 1
    assert strategy_step["diagnostics"][0]["status"] == "fallback_to_all"
    assert result["candidates"][0]["source"] == "alphasift:unit_breakout"


def test_discover_watchlist_candidates_auto_uses_candidate_experts(tmp_path):
    db_path = tmp_path / "sequoia.db"
    fundamental_db_path = tmp_path / "fundamental.db"
    strategy_dir = tmp_path / "strategies"
    _write_daily_db(db_path, _bars_for_turtle("600001") + _bars_for_alphasift_breakout("600003"))
    _write_alphasift_strategy_dir(strategy_dir)
    from src.agent.candidate_providers.fundamental_provider import ensure_fundamental_candidate_schema, upsert_fundamental_snapshots
    ensure_fundamental_candidate_schema(str(fundamental_db_path))
    upsert_fundamental_snapshots(
        [{
            "code": "600005",
            "name": "基本面候选",
            "report_period": "20251231",
            "roe": 19,
            "gross_margin": 45,
            "net_margin": 17,
            "revenue_growth": 24,
            "profit_growth": 38,
            "operating_cashflow_ratio": 95,
            "debt_ratio": 36,
            "pe_ttm": 24,
            "pb": 2.8,
        }],
        str(fundamental_db_path),
    )

    with patch.dict("os.environ", _isolated_candidate_env(tmp_path, SEQUOIA_CANDIDATE_DB_PATH=str(db_path), ALPHASIFT_CANDIDATE_DB_PATH=str(db_path), ALPHASIFT_STRATEGY_DIR=str(strategy_dir), AGENT_FUNDAMENTAL_CANDIDATE_DB_PATH=str(fundamental_db_path))), patch(
        "src.agent.tools.market_tools._top_sector_names",
        return_value=["半导体"],
    ), patch(
        "src.agent.tools.market_tools._fetch_sector_constituents",
        return_value=(
            [
                {
                    "code": "600002",
                    "name": "板块候选",
                    "source": "akshare:industry:半导体",
                    "reason": "来自强势板块。",
                    "change_pct": 5.0,
                }
            ],
            [{"source": "akshare:industry", "status": "ok", "sector": "半导体"}],
        ),
    ), patch(
        "src.agent.tools.market_tools._discover_event_impact_candidates",
        return_value={
            "status": "empty",
            "candidates": [],
            "events": [
                {
                    "event_id": "unit_event",
                    "title": "AI 产业事件仍待验证",
                    "watch_themes": ["人工智能"],
                    "maturity": "breaking",
                }
            ],
            "queries": [],
            "diagnostics": [],
        },
    ), patch(
        "src.agent.tools.market_tools._discover_news_momentum_candidates",
        return_value={"status": "empty", "candidates": [], "queries": [], "diagnostics": []},
    ), patch(
        "src.agent.tools.market_tools._data_tool_result",
        return_value={"status": "empty", "candidates": [], "items": [], "data": []},
    ), patch(
        "src.agent.tools.market_tools._stockapi_tool_result",
        return_value={"status": "empty", "candidates": [], "items": [], "data": []},
    ):
        result = _handle_discover_watchlist_candidates(
            candidate_source="auto",
            strategy_names=["turtle_trade", "unit_breakout"],
            limit=5,
        )

    assert result["status"] == "ok"
    assert result["candidate_source"] == "expert_graph_discovery"
    assert result["fallback_used"] is False
    assert result["expert_packets"]
    assert {packet["expert"] for packet in result["expert_packets"]} >= {
        "strategy_factor_expert",
        "technical_candidate_expert",
        "sector_theme_expert",
        "news_event_expert",
        "sentiment_theme_expert",
    }
    assert result["themes"][0]["theme"] == "人工智能"
    assert result["capacity"]["max_candidates_to_deep_dive"] == 5
    assert {item["code"] for item in result["candidates"]} == {"600001", "600002", "600003", "600005"}
    scores = [float(item.get("signal_score") or 0) for item in result["candidates"]]
    assert scores == sorted(scores, reverse=True)
    assert any(item.get("candidate_dimensions") == ["fundamental"] for item in result["candidates"])
    fundamental_packet = next(packet for packet in result["expert_packets"] if packet["expert"] == "fundamental_expert")
    assert fundamental_packet["status"] == "ok"
    assert fundamental_packet["candidates"][0]["code"] == "600005"
    assert any(item.get("candidate_experts") for item in result["candidates"])
    assert any(step["source"] == "candidate_expert:sector_theme_expert" for step in result["discovery_steps"])


def test_discover_watchlist_candidates_auto_merges_sequoia_and_sector(tmp_path):
    db_path = tmp_path / "sequoia.db"
    strategy_dir = tmp_path / "strategies"
    _write_daily_db(db_path, _bars_for_turtle("600001") + _bars_for_alphasift_breakout("600003"))
    _write_alphasift_strategy_dir(strategy_dir)

    with patch.dict("os.environ", _isolated_candidate_env(tmp_path, SEQUOIA_CANDIDATE_DB_PATH=str(db_path), ALPHASIFT_CANDIDATE_DB_PATH=str(db_path), ALPHASIFT_STRATEGY_DIR=str(strategy_dir))), patch(
        "src.agent.tools.market_tools._top_sector_names",
        return_value=["半导体"],
    ), patch(
        "src.agent.tools.market_tools._fetch_sector_constituents",
        return_value=(
            [
                {
                    "code": "600002",
                    "name": "板块候选",
                    "source": "akshare:industry:半导体",
                    "reason": "来自强势板块。",
                    "change_pct": 5.0,
                }
            ],
            [{"source": "akshare:industry", "status": "ok", "sector": "半导体"}],
        ),
    ) as fetch_sector, patch(
        "src.agent.tools.market_tools._discover_event_impact_candidates",
        return_value={"status": "empty", "candidates": [], "events": [], "queries": [], "diagnostics": []},
    ), patch(
        "src.agent.tools.market_tools._discover_news_momentum_candidates",
        return_value={"status": "empty", "candidates": [], "queries": [], "diagnostics": []},
    ), patch(
        "src.agent.tools.market_tools._data_tool_result",
        return_value={"status": "empty", "candidates": [], "items": [], "data": []},
    ), patch(
        "src.agent.tools.market_tools._stockapi_tool_result",
        return_value={"status": "empty", "candidates": [], "items": [], "data": []},
    ):
        result = _handle_discover_watchlist_candidates(
            candidate_source="auto",
            strategy_names=["turtle_trade", "unit_breakout"],
            limit=5,
        )

    assert result["status"] == "ok"
    assert result["candidate_source"] == "expert_graph_discovery"
    assert any(step["source"] == "candidate_expert:strategy_factor_expert" for step in result["discovery_steps"])
    assert any(step["source"] == "candidate_expert:technical_candidate_expert" for step in result["discovery_steps"])
    assert any(step["source"] == "candidate_expert:sector_theme_expert" for step in result["discovery_steps"])
    assert fetch_sector.called
    assert {item["code"] for item in result["candidates"]} == {"600001", "600002", "600003"}


def test_discover_watchlist_candidates_sector_uses_local_strategy_fallback_when_constituents_empty(tmp_path):
    db_path = tmp_path / "sequoia.db"
    strategy_dir = tmp_path / "strategies"
    _write_daily_db(db_path, _bars_for_turtle("600001") + _bars_for_alphasift_breakout("600003"))
    _write_alphasift_strategy_dir(strategy_dir)

    with patch.dict("os.environ", _isolated_candidate_env(tmp_path, SEQUOIA_CANDIDATE_DB_PATH=str(db_path), ALPHASIFT_CANDIDATE_DB_PATH=str(db_path), ALPHASIFT_STRATEGY_DIR=str(strategy_dir))), patch(
        "src.agent.tools.market_tools._fetch_sector_constituents",
        return_value=(
            [],
            [
                {
                    "source": "akshare:industry",
                    "status": "timeout",
                    "sector": "半导体",
                    "timeout_s": 3.0,
                }
            ],
        ),
    ):
        result = _handle_discover_watchlist_candidates(
            candidate_source="sector",
            sector_names=["半导体"],
            strategy_names=["turtle_trade", "unit_breakout"],
            limit=5,
        )

    assert result["status"] == "ok"
    assert result["candidate_source"] == "sector_local_fallback"
    assert result["fallback_used"] is False
    assert {item["code"] for item in result["candidates"]} == {"600001", "600003"}
    assert result["discovery_steps"][0]["source"] == "sector_constituents"
    assert result["discovery_steps"][0]["diagnostics"][0]["status"] == "timeout"
    assert any(step["source"] == "sector_local_fallback:alphasift" and step["count"] >= 1 for step in result["discovery_steps"])
    assert any(step["source"] == "sector_local_fallback:sequoia" and step["count"] >= 1 for step in result["discovery_steps"])
    assert not any(step["source"] == "fallback_seed_pool" for step in result["discovery_steps"])
    assert all(item["source"] != "fallback_seed_pool" for item in result["candidates"])


def test_discover_watchlist_candidates_sector_fallback_seed_only_after_local_sources_unavailable(tmp_path):
    with patch.dict("os.environ", _isolated_candidate_env(tmp_path, SEQUOIA_CANDIDATE_DB_PATH="/tmp/not-exists-sequoia.db", ALPHASIFT_CANDIDATE_DB_PATH="/tmp/not-exists-alphasift.db")), patch(
        "src.agent.tools.market_tools._fetch_sector_constituents",
        return_value=([], [{"source": "akshare:industry", "status": "empty", "sector": "半导体"}]),
    ):
        result = _handle_discover_watchlist_candidates(
            candidate_source="sector",
            sector_names=["半导体"],
            limit=2,
        )

    assert result["status"] == "partial"
    assert result["candidate_source"] == "fallback"
    assert result["fallback_used"] is True
    assert any(step["source"] == "sector_local_fallback:alphasift" for step in result["discovery_steps"])
    assert any(step["source"] == "sector_local_fallback:sequoia" for step in result["discovery_steps"])
    assert result["candidates"][0]["source"] == "fallback_seed_pool"


def test_discover_watchlist_candidates_keeps_source_diversity_when_limited():
    result = market_tools._dedupe_candidates(
        [
            {"code": "600001", "name": "Sequoia 一", "source": "sequoia:turtle_trade", "signal_score": 99},
            {"code": "600002", "name": "Sequoia 二", "source": "sequoia:turtle_trade", "signal_score": 98},
            {"code": "600003", "name": "Alpha 一", "source": "alphasift:volume_breakout", "signal_score": 80},
            {"code": "600004", "name": "板块一", "source": "akshare:industry:半导体", "signal_score": 70},
        ],
        limit=3,
    )

    sources = [item["source"] for item in result]
    assert sources[0] == "sequoia:turtle_trade"
    assert "alphasift:volume_breakout" in sources
    assert "akshare:industry:半导体" in sources


def test_discover_watchlist_candidates_merges_duplicate_multi_recall():
    result = market_tools._merge_and_score_candidates(
        [
            {
                "code": "600001",
                "name": "测试股票",
                "source": "sequoia:turtle_trade",
                "signal_score": 78,
                "matched_strategies": ["turtle_trade"],
                "strategy_tags": ["breakout"],
                "reason": "海龟突破。",
            },
            {
                "code": "600001",
                "name": "测试股票",
                "source": "akshare:industry:半导体",
                "change_pct": 6.0,
                "reason": "来自强势板块。",
            },
        ],
        limit=5,
    )

    assert len(result) == 1
    candidate = result[0]
    assert candidate["source"] == "multi_recall"
    assert candidate["recall_sources"] == ["sequoia:turtle_trade", "akshare:industry:半导体"]
    assert candidate["signal_score"] > 78
    assert candidate["matched_strategies"] == ["turtle_trade"]
    assert any(item["label"] == "策略" and "海龟突破" in item["detail"] for item in candidate["reason_dimensions"])
    assert any(item["label"] == "情绪/热点" and "半导体" in item["detail"] for item in candidate["reason_dimensions"])
    assert not any(item["label"] == "技术面" and "多策略共振" in item["detail"] for item in candidate["reason_dimensions"])


def test_discover_watchlist_candidates_resolves_display_names_from_stock_index():
    result = _handle_discover_watchlist_candidates(
        seed_symbols=["301183"],
        limit=1,
    )

    assert result["candidates"][0]["code"] == "301183"
    assert result["candidates"][0]["name"] == "东田微"


def test_discover_watchlist_candidates_falls_back_when_candidate_expert_sources_missing(tmp_path):
    with patch.dict("os.environ", _isolated_candidate_env(tmp_path, SEQUOIA_CANDIDATE_DB_PATH="/tmp/not-exists-sequoia.db", ALPHASIFT_CANDIDATE_DB_PATH="/tmp/not-exists-alphasift.db")), patch(
        "src.agent.tools.market_tools._top_sector_names",
        return_value=[],
    ), patch(
        "src.agent.tools.market_tools._discover_event_impact_candidates",
        return_value={"status": "empty", "candidates": [], "events": [], "queries": [], "diagnostics": []},
    ), patch(
        "src.agent.tools.market_tools._discover_news_momentum_candidates",
        return_value={"status": "empty", "candidates": [], "queries": [], "diagnostics": []},
    ):
        result = _handle_discover_watchlist_candidates(limit=2)

    assert result["status"] == "partial"
    assert result["candidate_source"] == "fallback"
    assert result["fallback_used"] is True
    strategy_step = next(step for step in result["discovery_steps"] if step["source"] == "candidate_expert:strategy_factor_expert")
    technical_step = next(step for step in result["discovery_steps"] if step["source"] == "candidate_expert:technical_candidate_expert")
    assert strategy_step["status"] == "unavailable"
    assert technical_step["status"] == "unavailable"
    assert result["candidates"][0]["source"] == "fallback_seed_pool"


def test_discover_watchlist_candidates_excludes_hard_risk_candidates(tmp_path):
    db_path = tmp_path / "sequoia.db"
    strategy_dir = tmp_path / "strategies"
    _write_daily_db(db_path, _bars_for_turtle("600001") + _bars_for_alphasift_breakout("600003"))
    _write_alphasift_strategy_dir(strategy_dir)

    with patch.dict("os.environ", _isolated_candidate_env(
        tmp_path,
        SEQUOIA_CANDIDATE_DB_PATH=str(db_path),
        ALPHASIFT_CANDIDATE_DB_PATH=str(db_path),
        ALPHASIFT_STRATEGY_DIR=str(strategy_dir),
        AGENT_CANDIDATE_BLACKLIST_CODES="600002",
    )), patch(
        "src.agent.tools.market_tools._top_sector_names",
        return_value=["半导体"],
    ), patch(
        "src.agent.tools.market_tools._fetch_sector_constituents",
        return_value=(
            [
                {
                    "code": "600002",
                    "name": "板块黑名单",
                    "source": "akshare:industry:半导体",
                    "reason": "来自强势板块。",
                    "change_pct": 5.0,
                },
                {
                    "code": "600004",
                    "name": "ST测试",
                    "source": "akshare:industry:半导体",
                    "reason": "来自强势板块。",
                    "change_pct": 4.0,
                },
            ],
            [{"source": "akshare:industry", "status": "ok", "sector": "半导体"}],
        ),
    ), patch(
        "src.agent.tools.market_tools._discover_event_impact_candidates",
        return_value={"status": "empty", "candidates": [], "events": [], "queries": [], "diagnostics": []},
    ), patch(
        "src.agent.tools.market_tools._discover_news_momentum_candidates",
        return_value={"status": "empty", "candidates": [], "queries": [], "diagnostics": []},
    ), patch(
        "src.agent.tools.market_tools._data_tool_result",
        return_value={"status": "empty", "candidates": [], "items": [], "data": []},
    ), patch(
        "src.agent.tools.market_tools._stockapi_tool_result",
        return_value={"status": "empty", "candidates": [], "items": [], "data": []},
    ):
        result = _handle_discover_watchlist_candidates(
            candidate_source="auto",
            strategy_names=["turtle_trade", "unit_breakout"],
            limit=5,
        )

    assert {item["code"] for item in result["candidates"]} == {"600001", "600003"}
    assert result["hard_exclusion"]["excluded_count"] == 2
    assert result["hard_exclusion"]["reason_counts"]["blacklisted"] == 1
    assert result["hard_exclusion"]["reason_counts"]["st_or_special_treatment"] == 1
    assert result["quality"]["hard_exclusion_count"] == 2
    assert result["quality"]["hard_strategy_trunk_missing"] is False
    hard_step = next(step for step in result["discovery_steps"] if step["source"] == "candidate_hard_exclusion")
    assert hard_step["count"] == 2


def test_discover_watchlist_candidates_reports_hard_strategy_trunk_missing_without_fallback():
    result = _candidate_discovery_response(
        status="ok",
        market="cn",
        candidates=[
            {
                "code": "600001",
                "name": "板块候选",
                "source": "akshare:industry:半导体",
                "reason": "强势板块成分股。",
                "reason_dimensions": [{"dimension": "sentiment", "label": "情绪/热点", "detail": "来自强势板块"}],
            }
        ],
        discovery_steps=[],
        fallback_used=False,
        candidate_source="sector",
        note="unit",
    )

    assert result["quality"]["hard_strategy_trunk_missing"] is True
    assert result["quality"]["source_counts"]["sector"] == 1
    assert result["hard_exclusion"]["excluded_count"] == 0


def test_discover_watchlist_candidates_auto_sanitizes_nan_metrics():
    with patch(
        "src.agent.tools.market_tools.CandidateExpertOrchestrator.discover",
        return_value={
            "status": "ok",
            "candidate_source": "expert_graph_discovery",
            "candidate_count": 1,
            "candidates": [
                {
                    "code": "688707",
                    "name": "振华新材",
                    "source": "fundamental:tushare_daily_basic",
                    "reason": "TuShare 日度估值/换手指标靠前，适合做基本面候选快照。",
                    "signal_score": 100,
                    "strategy_tags": ["fundamental", "daily_basic", "valuation"],
                    "metrics": {
                        "source_name": "tushare_daily_basic",
                        "trade_date": "20260522",
                        "pe_ttm": float("nan"),
                        "pb": 2.1529,
                    },
                    "raw_source_item": {
                        "ts_code": "688707.SH",
                        "trade_date": "20260522",
                        "pe": float("nan"),
                        "pe_ttm": float("nan"),
                        "pb": 2.1529,
                        "dv_ratio": float("nan"),
                    },
                }
            ],
            "expert_packets": [],
            "discovery_steps": [],
            "errors": [],
        },
    ):
        result = _handle_discover_watchlist_candidates(candidate_source="auto", limit=3)

    candidate = result["candidates"][0]
    assert candidate["metrics"]["pe_ttm"] is None
    assert candidate["raw_source_item"]["pe"] is None
    assert candidate["raw_source_item"]["pe_ttm"] is None
    assert candidate["raw_source_item"]["dv_ratio"] is None
