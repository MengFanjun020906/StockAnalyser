# -*- coding: utf-8 -*-
"""News event sentinel runtime module.

The public interface is NewsEventSentinel.run_once(). Everything else is an
adapter or policy used behind that seam so scheduler, tests and future Feishu
cards do not need to know source, cooldown or trigger details.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Protocol

import requests

from src.config import Config, get_config
from src.repositories.news_event_sentinel_repo import NewsEventSentinelRepository

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"low": 1, "mid": 2, "high": 3, "critical": 4}
_NEGATIVE_TONES = {"negative", "bearish", "risk", "bad", "利空", "harm"}
_POSITIVE_TONES = {"positive", "bullish", "good", "利好", "benefit"}
_HIGH_RISK_EVENT_TYPES = {
    "guidance_cut",
    "regulatory_penalty",
    "investigation",
    "default",
    "major_accident",
    "trading_halt",
    "earnings_warning",
    "shareholder_reduction",
}
_A_SHARE_MACRO_TERMS = (
    "A股",
    "A 股",
    "沪深",
    "上证",
    "深证",
    "创业板",
    "科创板",
    "北向",
    "人民币",
    "央行",
    "人民银行",
    "公开市场",
    "逆回购",
    "MLF",
    "LPR",
    "降准",
    "社融",
    "M2",
    "国内流动性",
    "货币政策",
    "财政政策",
)
_US_MACRO_TERMS = (
    "美股",
    "美国",
    "美联储",
    "FOMC",
    "FED",
    "非农",
    "失业率",
    "PCE",
    "美元指数",
    "美元",
    "美债",
    "纳指",
    "标普",
    "道指",
    "NASDAQ",
    "S&P",
    "TREASURY",
)


@dataclass(frozen=True)
class WatchedSymbol:
    symbol: str
    name: str = ""
    source: str = "watchlist"


@dataclass(frozen=True)
class WatchedUniverse:
    holdings: List[WatchedSymbol] = field(default_factory=list)
    watchlist: List[WatchedSymbol] = field(default_factory=list)
    candidate_symbols: List[WatchedSymbol] = field(default_factory=list)
    macro_queries: List[str] = field(default_factory=list)
    source_queries: List[str] = field(default_factory=list)
    symbol_aliases: Dict[str, List[str]] = field(default_factory=dict)
    loaded_at: datetime = field(default_factory=datetime.now)

    @property
    def watched_symbols(self) -> List[str]:
        symbols: List[str] = []
        seen: set[str] = set()
        for item in [*self.holdings, *self.watchlist, *self.candidate_symbols]:
            code = _canonical_symbol(item.symbol)
            if not code or code in seen:
                continue
            seen.add(code)
            symbols.append(code)
        return symbols

    @property
    def holding_symbols(self) -> set[str]:
        return {_canonical_symbol(item.symbol) for item in self.holdings if _canonical_symbol(item.symbol)}


@dataclass(frozen=True)
class SentinelNotificationEnvelope:
    title: str
    severity: str
    direction: str
    symbols: List[str]
    summary: str
    why_triggered: List[str]
    source_count: int
    first_seen_at: Optional[datetime]
    card_id: str
    event_id: Optional[str]
    trace_id: Optional[str]
    graph_trace: Dict[str, Any]
    links: List[Dict[str, str]]
    transmission_paths: List[Dict[str, Any]]
    diagnostics: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if isinstance(self.first_seen_at, datetime):
            data["first_seen_at"] = self.first_seen_at.isoformat()
        return data


class UniverseProvider(Protocol):
    def load(self, *, now: datetime) -> WatchedUniverse:
        ...


class CardProvider(Protocol):
    def fetch_cards(self, *, universe: WatchedUniverse, now: datetime, limit: int) -> Dict[str, Any]:
        ...


class SentinelNotifier(Protocol):
    def send(self, envelope: SentinelNotificationEnvelope) -> Dict[str, Any]:
        ...


class GraphTraceProvider(Protocol):
    def trace(self, card: Dict[str, Any], decision: Dict[str, Any]) -> Dict[str, Any]:
        ...


class NoopSentinelNotifier:
    def send(self, envelope: SentinelNotificationEnvelope) -> Dict[str, Any]:
        return {"status": "skipped", "channel": "noop"}


class FeishuSentinelNotifier:
    """Send sentinel notification envelopes to a Feishu custom bot webhook."""

    def __init__(
        self,
        config: Optional[Config] = None,
        *,
        webhook_url: Optional[str] = None,
        secret: Optional[str] = None,
        keyword: Optional[str] = None,
        timeout_seconds: int = 30,
    ) -> None:
        self.config = config or get_config()
        self.enabled = bool(getattr(self.config, "news_event_sentinel_feishu_enabled", False))
        self.webhook_url = (webhook_url or getattr(self.config, "feishu_webhook_url", None) or "").strip()
        self.secret = (secret if secret is not None else getattr(self.config, "feishu_webhook_secret", None) or "").strip()
        self.keyword = (keyword if keyword is not None else getattr(self.config, "feishu_webhook_keyword", None) or "").strip()
        self.timeout_seconds = _bounded_int(timeout_seconds, 1, 60, 30)
        self.verify_ssl = bool(getattr(self.config, "webhook_verify_ssl", True))

    def send(self, envelope: SentinelNotificationEnvelope) -> Dict[str, Any]:
        if not self.enabled:
            return {"status": "skipped", "channel": "feishu_webhook", "reason": "disabled"}
        if not self.webhook_url:
            return {"status": "skipped", "channel": "feishu_webhook", "reason": "missing_webhook_url"}

        payload = self._build_payload(envelope)
        payload.update(self._build_security_fields())
        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                timeout=self.timeout_seconds,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            logger.warning("feishu sentinel webhook request failed: %s", exc)
            return {
                "status": "failed",
                "channel": "feishu_webhook",
                "error": str(exc),
            }

        result: Dict[str, Any] = {}
        try:
            result = response.json() if response.text else {}
        except ValueError:
            result = {"raw": response.text[:500]}

        code = result.get("code") if "code" in result else result.get("StatusCode")
        if response.status_code == 200 and code == 0:
            return {
                "status": "sent",
                "channel": "feishu_webhook",
                "http_status": response.status_code,
                "feishu_code": code,
            }

        return {
            "status": "failed",
            "channel": "feishu_webhook",
            "http_status": response.status_code,
            "feishu_code": code,
            "error": result.get("msg") or result.get("StatusMessage") or response.text[:500],
        }

    def _build_security_fields(self) -> Dict[str, str]:
        if not self.secret:
            return {}
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{self.secret}"
        sign = base64.b64encode(
            hmac.new(
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        return {"timestamp": timestamp, "sign": sign}

    def _build_payload(self, envelope: SentinelNotificationEnvelope) -> Dict[str, Any]:
        title = _truncate_text(envelope.title or "StockAnalyser 新闻哨兵", 120)
        content = self._build_markdown(envelope)
        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": _feishu_template_for(envelope),
                    "title": {"tag": "plain_text", "content": title},
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": content},
                    }
                ],
            },
        }

    def _build_markdown(self, envelope: SentinelNotificationEnvelope) -> str:
        lines: List[str] = []
        if self.keyword:
            lines.append(self.keyword)
        lines.extend(
            [
                f"**摘要**\n{_truncate_text(envelope.summary, 500)}",
                f"**标的**：{', '.join(envelope.symbols) or '-'}",
                f"**方向/等级**：{envelope.direction} / {envelope.severity}",
                f"**触发原因**：{'、'.join(envelope.why_triggered) or '-'}",
                f"**来源数**：{envelope.source_count}",
            ]
        )
        if envelope.first_seen_at:
            lines.append(f"**首次发现**：{envelope.first_seen_at.isoformat(timespec='seconds')}")

        paths = [_format_transmission_path(item) for item in envelope.transmission_paths[:3]]
        paths = [item for item in paths if item]
        if paths:
            lines.append("**关联传导路径**\n" + "\n".join(f"- {item}" for item in paths))

        graph_trace = _format_graph_trace(envelope.graph_trace)
        if graph_trace:
            lines.append("**Graphiti 事件跟踪**\n" + graph_trace)

        links = [_format_source_link(item) for item in envelope.links[:3]]
        links = [item for item in links if item]
        if links:
            lines.append("**来源**\n" + "\n".join(f"- {item}" for item in links))

        identifiers = []
        if envelope.card_id:
            identifiers.append(f"card={envelope.card_id}")
        if envelope.event_id:
            identifiers.append(f"event={envelope.event_id}")
        if identifiers:
            lines.append("**审计**：" + "，".join(identifiers))
        return "\n\n".join(lines)


def _build_default_notifier(config: Config) -> SentinelNotifier:
    if bool(getattr(config, "news_event_sentinel_feishu_enabled", False)):
        return FeishuSentinelNotifier(config)
    return NoopSentinelNotifier()


class ConfigWatchedUniverseProvider:
    """Build the first sentinel watched universe from StockAnalyser config."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or get_config()

    def load(self, *, now: datetime) -> WatchedUniverse:
        holdings = [WatchedSymbol(symbol=symbol, source="portfolio") for symbol in _load_portfolio_holding_symbols()]
        holding_codes = {_canonical_symbol(item.symbol) for item in holdings}
        watchlist = [
            WatchedSymbol(symbol=str(code).strip(), source="stock_list")
            for code in getattr(self.config, "stock_list", []) or []
            if str(code).strip() and _canonical_symbol(code) not in holding_codes
        ]
        symbols = [
            _canonical_symbol(item.symbol)
            for item in [*holdings, *watchlist]
            if _canonical_symbol(item.symbol)
        ]
        source_queries = [f"{symbol} news" for symbol in symbols]
        macro_queries = [
            "A股 货币政策 流动性",
            "央行 逆回购 利率",
            "监管 政策 股市",
            "CPI inflation report",
            "Fed FOMC rate decision",
            "US non-farm payrolls jobs report",
            "PPI producer price index",
        ]
        return WatchedUniverse(
            holdings=holdings,
            watchlist=watchlist,
            candidate_symbols=[],
            macro_queries=macro_queries,
            source_queries=[*source_queries, *macro_queries],
            symbol_aliases={symbol: [symbol] for symbol in symbols},
            loaded_at=now,
        )


class NewsSignalCardProvider:
    """Adapter that reuses the existing news signal ingestion path."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or get_config()

    def fetch_cards(self, *, universe: WatchedUniverse, now: datetime, limit: int) -> Dict[str, Any]:
        from src.services.news_signal_service import NewsSignalService

        service = NewsSignalService()
        ingest = service.ingest_cls_incremental(limit=limit)
        errors = list(ingest.get("errors") or []) if isinstance(ingest, dict) else []
        cards_result = service.list_cards(signal_date=now.date().isoformat(), limit=limit)
        cards = list(cards_result.get("items") or []) if isinstance(cards_result, dict) else []
        portfolio_cards = _recent_portfolio_anysearch_cards(
            service,
            config=self.config,
            now=now,
            limit=limit,
        )
        cards = _merge_cards(cards, portfolio_cards, limit=limit)
        ingest_status = str(ingest.get("status", "ok") if isinstance(ingest, dict) else "ok")
        if ingest_status == "empty" and cards and not errors:
            ingest_status = "ok"
        return {
            "status": ingest_status,
            "fetched_count": int(ingest.get("fetched_items") or len(cards)) if isinstance(ingest, dict) else len(cards),
            "unseen_count": int(ingest.get("new_raw_episodes") or 0) if isinstance(ingest, dict) else 0,
            "raw_episode_count": int(ingest.get("new_raw_episodes") or 0) if isinstance(ingest, dict) else 0,
            "card_count": len(cards),
            "cards": cards,
            "errors": errors,
            "diagnostics": {
                "source": "news_signal_cls_incremental",
                "ingest": ingest,
                "portfolio_anysearch_cards": len(portfolio_cards),
            },
        }


class GraphitiSentinelTraceProvider:
    """Resolve best-effort Graphiti event context for a sentinel card."""

    def __init__(self, config: Optional[Config] = None, *, graphiti: Any = None) -> None:
        self.config = config or get_config()
        self._graphiti = graphiti

    def trace(self, card: Dict[str, Any], decision: Dict[str, Any]) -> Dict[str, Any]:
        query = _graphiti_trace_query(card, decision)
        if not query:
            return {"status": "skipped", "reason": "empty_query"}

        trace_id = _stable_id("graphiti-trace", card.get("card_id"), query)
        timeout_seconds = _bounded_float(
            getattr(self.config, "graphiti_selection_search_timeout_seconds", 12.0),
            default=12.0,
            minimum=1.0,
            maximum=30.0,
        )
        try:
            graphiti = self._graphiti
            if graphiti is None:
                from src.services.graphiti.graph_service import get_graphiti_service

                graphiti = get_graphiti_service()
                self._graphiti = graphiti
            result = graphiti.search_sync(
                query,
                market="cn",
                limit=5,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            logger.warning("Graphiti sentinel trace failed for card=%s: %s", card.get("card_id"), exc, exc_info=True)
            return {
                "status": "failed",
                "trace_id": trace_id,
                "query": query,
                "error": str(exc),
            }

        if not isinstance(result, dict):
            return {
                "status": "failed",
                "trace_id": trace_id,
                "query": query,
                "error": "invalid_graphiti_result",
            }

        raw_edges = result.get("edges") if isinstance(result.get("edges"), list) else []
        raw_nodes = result.get("nodes") if isinstance(result.get("nodes"), list) else []
        raw_episodes = result.get("episodes") if isinstance(result.get("episodes"), list) else []
        edges, nodes, episodes, filtered_out = _filter_graph_trace_results(
            card,
            decision,
            edges=raw_edges,
            nodes=raw_nodes,
            episodes=raw_episodes,
        )
        degraded = bool(result.get("degraded")) or str(result.get("source") or "") == "relational_fallback"
        success = bool(result.get("success", True))
        status = "linked"
        if not success:
            status = "failed"
        elif degraded:
            status = "degraded"
        elif not edges and not nodes and not episodes:
            status = "empty"
        fallback_reason = result.get("fallback_reason")
        if status == "empty" and filtered_out:
            fallback_reason = "filtered_weak_relevance"

        return {
            "status": status,
            "trace_id": trace_id,
            "query": query,
            "source": result.get("source") or ("graphiti" if not degraded else "relational_fallback"),
            "fallback_reason": fallback_reason,
            "graphiti_error": result.get("graphiti_error"),
            "edges": edges[:5],
            "nodes": nodes[:5],
            "episodes": episodes[:5],
            "edge_count": len(edges),
            "node_count": len(nodes),
            "episode_count": len(episodes),
            "raw_edge_count": len(raw_edges),
            "raw_node_count": len(raw_nodes),
            "raw_episode_count": len(raw_episodes),
            "filtered_out_count": filtered_out,
        }


class NewsEventSentinel:
    """Run the event sentinel once and return a structured run summary."""

    def __init__(
        self,
        *,
        config: Optional[Config] = None,
        repository: Optional[NewsEventSentinelRepository] = None,
        universe_provider: Optional[UniverseProvider] = None,
        card_provider: Optional[CardProvider] = None,
        notifier: Optional[SentinelNotifier] = None,
        trace_provider: Optional[GraphTraceProvider] = None,
    ) -> None:
        self.config = config or get_config()
        self.repository = repository or NewsEventSentinelRepository()
        self.universe_provider = universe_provider or ConfigWatchedUniverseProvider(self.config)
        self.card_provider = card_provider or NewsSignalCardProvider(self.config)
        self.notifier = notifier or _build_default_notifier(self.config)
        self.trace_provider = trace_provider or GraphitiSentinelTraceProvider(self.config)

    def run_once(self, *, now: Optional[datetime] = None, dry_run: bool = False) -> Dict[str, Any]:
        current = now or datetime.now()
        run_id = _stable_id("sentinel-run", current.isoformat(timespec="seconds"))[:32]
        started_at = current
        status = "ok"
        errors: List[Any] = []
        diagnostics: Dict[str, Any] = {}
        triggers: List[Dict[str, Any]] = []
        envelopes: List[Dict[str, Any]] = []
        suppressed_by_cooldown = 0
        stale_card_suppressed = 0
        traces_used = 0

        universe = self.universe_provider.load(now=current)
        watched_symbols = set(universe.watched_symbols)
        source_query_count = len(universe.source_queries)
        active_windows = str(getattr(self.config, "news_event_sentinel_active_windows", "08:00-02:30") or "").strip()
        if not dry_run and not _is_in_active_windows(current, active_windows):
            status = "skipped_inactive_window"
            run = self.repository.record_run(
                {
                    "run_id": run_id,
                    "started_at": started_at,
                    "finished_at": current,
                    "status": status,
                    "watched_symbol_count": len(watched_symbols),
                    "source_query_count": source_query_count,
                    "fetched_count": 0,
                    "unseen_count": 0,
                    "raw_episode_count": 0,
                    "card_count": 0,
                    "trigger_count": 0,
                    "suppressed_by_cooldown": 0,
                    "errors": [],
                    "diagnostics": {"active_windows": active_windows},
                }
            )
            return {
                "status": status,
                "run_id": run_id,
                "run": run,
                "watched_symbols": sorted(watched_symbols),
                "fetched": 0,
                "cards_scanned": 0,
                "triggered": 0,
                "suppressed_by_cooldown": 0,
                "triggers": [],
                "envelopes": [],
                "errors": [],
                "dry_run": dry_run,
            }

        source_result: Dict[str, Any] = {}
        try:
            limit = _bounded_int(getattr(self.config, "news_event_sentinel_max_items_per_source", 20), 1, 100, 20)
            source_result = self.card_provider.fetch_cards(universe=universe, now=current, limit=limit)
            source_status = str(source_result.get("status") or "ok")
            errors.extend(source_result.get("errors") or [])
            diagnostics.update(source_result.get("diagnostics") or {})
            if source_status == "failed":
                status = "failed"
            elif source_status not in {"ok", "success"} or errors:
                status = "partial"
            trigger_mode = str(getattr(self.config, "news_event_sentinel_trigger_mode", "notify_only") or "notify_only").lower()
            diagnostics["trigger_mode"] = trigger_mode
            cards = [card for card in source_result.get("cards") or [] if isinstance(card, dict)]
            if trigger_mode == "disabled":
                cards = []
            for card in cards:
                if self._is_card_stale(card, current):
                    stale_card_suppressed += 1
                    continue
                decision = self._decide_card(card, universe, current)
                if not decision:
                    continue
                cooldown_key = decision["cooldown_key"]
                cooldown_since = current - timedelta(
                    minutes=_bounded_int(getattr(self.config, "news_event_sentinel_cooldown_minutes", 120), 1, 1440, 120)
                )
                previous = self.repository.latest_trigger_for_cooldown(cooldown_key, since=cooldown_since)
                if previous and not decision.get("cooldown_breaker"):
                    suppressed_by_cooldown += 1
                    continue

                graph_trace = {"status": "skipped", "reason": "trace_budget_exhausted"}
                trace_max_per_run = _bounded_int(
                    getattr(self.config, "news_event_sentinel_trace_max_per_run", 2),
                    0,
                    20,
                    2,
                )
                if traces_used < trace_max_per_run:
                    graph_trace = self._resolve_graph_trace(card, decision)
                    traces_used += 1
                envelope = _build_envelope(card, decision, graph_trace=graph_trace)
                notification_status = "skipped"
                notification_result: Dict[str, Any] = {"status": "skipped"}
                if not dry_run:
                    try:
                        notification_result = self.notifier.send(envelope)
                        notification_status = str(notification_result.get("status") or "unknown")
                    except Exception as exc:  # pragma: no cover - defensive logging path
                        notification_status = "failed"
                        notification_result = {"status": "failed", "error": str(exc)}
                        errors.append({"stage": "notify", "error": str(exc), "card_id": card.get("card_id")})
                        logger.warning("news event sentinel notification failed: %s", exc, exc_info=True)
                else:
                    notification_status = "dry_run"
                    notification_result = {"status": "dry_run"}

                trigger_payload = {
                    "trigger_id": _stable_id("sentinel-trigger", run_id, cooldown_key),
                    "run_id": run_id,
                    "card_id": str(card.get("card_id") or ""),
                    "event_id": decision.get("event_id") or "",
                    "canonical_symbol": decision["symbol"],
                    "event_type": decision["event_type"],
                    "direction": decision["direction"],
                    "severity": decision["severity"],
                    "cooldown_key": cooldown_key,
                    "triggered_at": current,
                    "notification_status": notification_status,
                    "trace_status": str(graph_trace.get("status") or "skipped"),
                    "notification_payload": envelope.to_dict(),
                    "diagnostics": {
                        "why_triggered": decision["why_triggered"],
                        "notification_result": notification_result,
                        "graph_trace": graph_trace,
                    },
                }
                saved = self.repository.record_trigger(trigger_payload)
                triggers.append(saved)
                envelopes.append(envelope.to_dict())

            diagnostics["stale_card_suppressed"] = stale_card_suppressed
            diagnostics["graph_trace_used"] = traces_used
            heartbeat = self._maybe_emit_heartbeat(
                run_id=run_id,
                current=current,
                universe=universe,
                source_result=source_result,
                errors=errors,
                suppressed_by_cooldown=suppressed_by_cooldown,
                stale_card_suppressed=stale_card_suppressed,
                trigger_mode=trigger_mode,
                dry_run=dry_run,
                market_trigger_count=len(triggers),
            )
            if heartbeat:
                triggers.append(heartbeat["trigger"])
                envelopes.append(heartbeat["envelope"])
        except Exception as exc:
            status = "failed"
            errors.append({"stage": "run", "error": str(exc)})
            logger.warning("news event sentinel run failed: %s", exc, exc_info=True)

        finished_at = now or datetime.now()
        run = self.repository.record_run(
            {
                "run_id": run_id,
                "started_at": started_at,
                "finished_at": finished_at,
                "status": status,
                "watched_symbol_count": len(watched_symbols),
                "source_query_count": source_query_count,
                "fetched_count": source_result.get("fetched_count", 0),
                "unseen_count": source_result.get("unseen_count", 0),
                "raw_episode_count": source_result.get("raw_episode_count", 0),
                "card_count": source_result.get("card_count", 0),
                "trigger_count": len(triggers),
                "suppressed_by_cooldown": suppressed_by_cooldown,
                "errors": errors,
                "diagnostics": diagnostics,
            }
        )
        return {
            "status": status,
            "run_id": run_id,
            "run": run,
            "watched_symbols": sorted(watched_symbols),
            "fetched": int(source_result.get("fetched_count", 0) or 0),
            "cards_scanned": int(source_result.get("card_count", 0) or 0),
            "triggered": len(triggers),
            "suppressed_by_cooldown": suppressed_by_cooldown,
            "triggers": triggers,
            "envelopes": envelopes,
            "errors": errors,
            "dry_run": dry_run,
        }

    def _maybe_emit_heartbeat(
        self,
        *,
        run_id: str,
        current: datetime,
        universe: WatchedUniverse,
        source_result: Dict[str, Any],
        errors: List[Any],
        suppressed_by_cooldown: int,
        stale_card_suppressed: int,
        trigger_mode: str,
        dry_run: bool,
        market_trigger_count: int,
    ) -> Optional[Dict[str, Any]]:
        if market_trigger_count > 0 or trigger_mode == "disabled":
            return None
        if not bool(getattr(self.config, "news_event_sentinel_heartbeat_enabled", False)):
            return None

        interval_minutes = _bounded_int(
            getattr(self.config, "news_event_sentinel_heartbeat_interval_minutes", 60),
            5,
            1440,
            60,
        )
        cooldown_key = _stable_id("sentinel-heartbeat", "SENTINEL:HEARTBEAT")
        since = current - timedelta(minutes=interval_minutes)
        if self.repository.latest_trigger_for_cooldown(cooldown_key, since=since):
            return None

        envelope = _build_heartbeat_envelope(
            run_id=run_id,
            current=current,
            universe=universe,
            source_result=source_result,
            errors=errors,
            suppressed_by_cooldown=suppressed_by_cooldown,
            stale_card_suppressed=stale_card_suppressed,
        )
        notification_status = "skipped"
        notification_result: Dict[str, Any] = {"status": "skipped"}
        if not dry_run:
            try:
                notification_result = self.notifier.send(envelope)
                notification_status = str(notification_result.get("status") or "unknown")
            except Exception as exc:  # pragma: no cover - defensive logging path
                notification_status = "failed"
                notification_result = {"status": "failed", "error": str(exc)}
                errors.append({"stage": "heartbeat_notify", "error": str(exc), "run_id": run_id})
                logger.warning("news event sentinel heartbeat notification failed: %s", exc, exc_info=True)
        else:
            notification_status = "dry_run"
            notification_result = {"status": "dry_run"}

        trigger_payload = {
            "trigger_id": _stable_id("sentinel-trigger", run_id, cooldown_key),
            "run_id": run_id,
            "card_id": envelope.card_id,
            "event_id": "",
            "canonical_symbol": "SENTINEL:HEARTBEAT",
            "event_type": "heartbeat",
            "direction": "neutral",
            "severity": "low",
            "cooldown_key": cooldown_key,
            "triggered_at": current,
            "notification_status": notification_status,
            "trace_status": "skipped",
            "notification_payload": envelope.to_dict(),
            "diagnostics": {
                "why_triggered": envelope.why_triggered,
                "notification_result": notification_result,
            },
        }
        return {
            "trigger": self.repository.record_trigger(trigger_payload),
            "envelope": envelope.to_dict(),
        }

    def _resolve_graph_trace(self, card: Dict[str, Any], decision: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return self.trace_provider.trace(card, decision)
        except Exception as exc:  # pragma: no cover - provider-level defensive path
            logger.warning("news event sentinel graph trace provider failed: %s", exc, exc_info=True)
            return {"status": "failed", "error": str(exc)}

    def _is_card_stale(self, card: Dict[str, Any], now: datetime) -> bool:
        event_time = _card_freshness_time(card)
        if _is_portfolio_anysearch_card(card):
            if event_time is None:
                return True
            if event_time.tzinfo is not None:
                event_time = event_time.replace(tzinfo=None)
            max_age_days = _portfolio_anysearch_max_age_days(self.config)
            if event_time > now + timedelta(minutes=5):
                return False
            return now - event_time > timedelta(days=max_age_days)
        if event_time is None:
            return False
        if event_time.tzinfo is not None:
            event_time = event_time.replace(tzinfo=None)
        max_age_minutes = _bounded_int(
            getattr(self.config, "news_event_sentinel_card_max_age_minutes", 30),
            5,
            1440,
            30,
        )
        if event_time > now + timedelta(minutes=5):
            return False
        return now - event_time > timedelta(minutes=max_age_minutes)

    def _decide_card(self, card: Dict[str, Any], universe: WatchedUniverse, now: datetime) -> Optional[Dict[str, Any]]:
        if str(card.get("status") or "active") != "active":
            return None
        evidence_grade = str(card.get("evidence_grade") or "plausible")
        is_macro = str(card.get("signal_layer") or "").lower() == "macro"
        broad_signal_direction = _broad_signal_direction(card)
        if evidence_grade == "speculative" and not broad_signal_direction:
            return None
        if float(card.get("mapping_confidence") or 0.0) < 0.35 and not is_macro and not broad_signal_direction:
            return None

        direction = _card_direction(card)
        if direction == "neutral":
            return None

        event = _primary_event(card)
        event_type = str(event.get("event_type") or "unknown")
        severity = _severity_for_card(card, event_type)
        min_severity = str(getattr(self.config, "news_event_sentinel_min_severity", "mid") or "mid").lower()
        if _SEVERITY_RANK.get(severity, 1) < _SEVERITY_RANK.get(min_severity, 2):
            return None

        symbols = _card_symbols(card)
        watched = set(universe.watched_symbols)
        matched = [symbol for symbol in symbols if symbol in watched]
        if matched:
            return _company_decision(card, universe, matched[0], event, event_type, direction, severity, evidence_grade)
        if is_macro:
            return _macro_decision(card, event, event_type, direction, severity, evidence_grade)
        if broad_signal_direction and broad_signal_direction == direction:
            return _directional_signal_decision(card, event, event_type, direction, severity, evidence_grade)
        return None


def _company_decision(
    card: Dict[str, Any],
    universe: WatchedUniverse,
    symbol: str,
    event: Dict[str, Any],
    event_type: str,
    direction: str,
    severity: str,
    evidence_grade: str,
) -> Dict[str, Any]:
    why = ["关注命中"]
    if symbol in universe.holding_symbols:
        why.insert(0, "持仓命中")
    _append_common_trigger_reasons(why, card, severity, evidence_grade)

    summary = str(card.get("summary_short") or "")
    cooldown_basis = summary[:120] or str(card.get("card_id") or "")
    cooldown_key = _stable_id("sentinel-cooldown", symbol, event_type, direction, cooldown_basis)
    return {
        "symbol": symbol,
        "symbols": [symbol],
        "event_id": str(event.get("event_id") or ""),
        "event_type": event_type,
        "direction": direction,
        "severity": severity,
        "cooldown_key": cooldown_key,
        "why_triggered": why,
        "cooldown_breaker": False,
    }


def _directional_signal_decision(
    card: Dict[str, Any],
    event: Dict[str, Any],
    event_type: str,
    direction: str,
    severity: str,
    evidence_grade: str,
) -> Dict[str, Any]:
    is_negative = direction == "negative"
    symbol = "SIGNAL:NEGATIVE" if is_negative else "SIGNAL:POSITIVE"
    theme = _primary_theme_for_card(card)
    why = ["负向避险线索" if is_negative else "正向线索"]
    if theme:
        why.append(f"主题={theme}")
    _append_common_trigger_reasons(why, card, severity, evidence_grade)

    summary = str(card.get("summary_short") or "")
    cooldown_basis = _directional_signal_cooldown_basis(card, summary)
    fallback_event_type = "negative_signal" if is_negative else "positive_signal"
    normalized_event_type = event_type if event_type != "unknown" else fallback_event_type
    cooldown_key = _stable_id("sentinel-cooldown", symbol, normalized_event_type, direction, cooldown_basis)
    return {
        "symbol": symbol,
        "symbols": [symbol],
        "event_id": str(event.get("event_id") or ""),
        "event_type": normalized_event_type,
        "direction": direction,
        "severity": severity,
        "cooldown_key": cooldown_key,
        "why_triggered": why,
        "cooldown_breaker": False,
    }


def _broad_signal_direction(card: Dict[str, Any]) -> str:
    if str(card.get("status") or "active") != "active":
        return ""
    if str(card.get("signal_layer") or "").lower() == "macro":
        return ""
    direction = _normalize_direction(card.get("news_tone"))
    if direction not in {"positive", "negative"}:
        return ""
    if float(card.get("signal_score") or 0.0) < 50.0:
        return ""
    return direction


def _directional_signal_cooldown_basis(card: Dict[str, Any], summary: str) -> str:
    special_basis = _special_directional_signal_cooldown_basis(summary)
    if special_basis:
        return special_basis
    cluster_id = _event_cluster_id(card)
    if cluster_id:
        return f"event_cluster:{cluster_id}"
    normalized = _normalize_directional_signal_summary(summary)
    return normalized[:160] or str(card.get("card_id") or "")


def _event_cluster_id(card: Dict[str, Any]) -> str:
    diagnostics = card.get("diagnostics") if isinstance(card.get("diagnostics"), dict) else {}
    cluster = diagnostics.get("event_cluster") if isinstance(diagnostics.get("event_cluster"), dict) else {}
    return str(cluster.get("cluster_id") or "").strip()


def _normalize_directional_signal_summary(summary: str) -> str:
    text = re.sub(r"\s+", "", str(summary or ""))
    if not text:
        return ""
    text = re.sub(r"财联社\d{1,2}月\d{1,2}日电", "", text)
    text = re.sub(
        r"\d+(?:\.\d+)?\s*(?:万亿|亿元|亿|万元|万|元|美元|港元|%|个|家|只|点|GWh|MWh|GW|MW)",
        "#",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\d+(?:\.\d+)?", "#", text)
    return text


def _special_directional_signal_cooldown_basis(summary: str) -> str:
    text = re.sub(r"\s+", "", str(summary or ""))
    if "成交额突破" in text and any(term in text for term in ("沪深两市", "两市", "A股", "A 股")):
        return "market_turnover_breakthrough:沪深两市成交额突破"
    return ""


def _primary_theme_for_card(card: Dict[str, Any]) -> str:
    values = card.get("primary_industries") if isinstance(card.get("primary_industries"), list) else []
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return str(card.get("signal_layer") or "").strip()


def _macro_decision(
    card: Dict[str, Any],
    event: Dict[str, Any],
    event_type: str,
    direction: str,
    severity: str,
    evidence_grade: str,
) -> Optional[Dict[str, Any]]:
    markets = _macro_markets_for_card(card)
    if not markets:
        return None
    symbol = markets[0]
    why = ["宏观命中"]
    if "MACRO:A_SHARE" in markets:
        why.append("A股宏观")
    if "MACRO:US" in markets:
        why.append("美股宏观")
    _append_common_trigger_reasons(why, card, severity, evidence_grade)

    summary = str(card.get("summary_short") or "")
    cooldown_basis = summary[:160] or str(card.get("card_id") or "")
    cooldown_key = _stable_id("sentinel-cooldown", ",".join(markets), event_type, direction, cooldown_basis)
    return {
        "symbol": symbol,
        "symbols": markets,
        "event_id": str(event.get("event_id") or ""),
        "event_type": event_type if event_type != "unknown" else "macro_event",
        "direction": direction,
        "severity": severity,
        "cooldown_key": cooldown_key,
        "why_triggered": why,
        "cooldown_breaker": False,
    }


def _append_common_trigger_reasons(why: List[str], card: Dict[str, Any], severity: str, evidence_grade: str) -> None:
    diagnostics = card.get("diagnostics") if isinstance(card.get("diagnostics"), dict) else {}
    quality = diagnostics.get("positive_signal_quality") if isinstance(diagnostics.get("positive_signal_quality"), dict) else {}
    quality_category = str(quality.get("category") or "")
    quality_label = str(quality.get("label") or "")
    if quality_category and quality_category != "unclassified" and quality_label:
        why.append(f"利好质量={quality_label}")
    if severity in {"high", "critical"}:
        why.append(f"severity={severity}")
    if int(card.get("source_count") or 0) > 1:
        why.append(f"来源数={int(card.get('source_count') or 0)}")
    if evidence_grade:
        why.append(f"证据={evidence_grade}")


def _macro_markets_for_card(card: Dict[str, Any]) -> List[str]:
    text = _card_text(card)
    upper_text = text.upper()
    markets: List[str] = []
    if _has_a_share_macro_signal(text, upper_text):
        markets.append("MACRO:A_SHARE")
    if any(term.upper() in upper_text for term in _US_MACRO_TERMS):
        markets.append("MACRO:US")
    return _unique(markets)


def _has_a_share_macro_signal(text: str, upper_text: str) -> bool:
    strong_terms = tuple(term for term in _A_SHARE_MACRO_TERMS if term not in {"央行", "货币政策", "财政政策"})
    if any(term.upper() in upper_text for term in strong_terms):
        return True
    central_bank_operation_terms = ("公开市场", "逆回购", "MLF", "LPR", "降准", "社融", "M2", "净投放", "净回笼")
    if "央行" in text and any(term.upper() in upper_text for term in central_bank_operation_terms):
        return True
    policy_context_terms = ("中国", "国内", "A股", "A 股", "人民币", "沪深")
    if any(term in text for term in ("货币政策", "财政政策")) and any(term in text for term in policy_context_terms):
        return True
    return False


def _card_text(card: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("summary_short", "news_tone", "market_impact", "signal_layer"):
        value = card.get(key)
        if value:
            parts.append(str(value))
    for key in ("primary_industries", "secondary_industries", "explicit_entities"):
        values = card.get(key) if isinstance(card.get(key), list) else []
        parts.extend(str(item) for item in values if str(item).strip())
    for event in card.get("extracted_events") or []:
        if isinstance(event, dict):
            parts.extend(str(event.get(key) or "") for key in ("event_type", "evidence_sentence", "direction"))
    for item in card.get("source_chain") or []:
        if isinstance(item, dict):
            parts.extend(str(item.get(key) or "") for key in ("title", "source"))
    return " ".join(part for part in parts if part)


def _load_portfolio_holding_symbols() -> List[str]:
    try:
        from sqlalchemy import and_, select

        from src.storage import DatabaseManager, PortfolioAccount, PortfolioPosition
    except Exception:
        return []

    try:
        db = DatabaseManager.get_instance()
        with db.get_session() as session:
            rows = session.execute(
                select(PortfolioPosition.symbol)
                .join(PortfolioAccount, PortfolioPosition.account_id == PortfolioAccount.id)
                .where(
                    and_(
                        PortfolioAccount.is_active.is_(True),
                        PortfolioPosition.quantity > 0,
                    )
                )
                .order_by(PortfolioPosition.symbol.asc())
            ).all()
            return _unique([_canonical_symbol(row[0]) for row in rows if _canonical_symbol(row[0])])
    except Exception as exc:
        logger.debug("portfolio holdings unavailable for news event sentinel: %s", exc)
        return []


def _build_envelope(
    card: Dict[str, Any],
    decision: Dict[str, Any],
    *,
    graph_trace: Optional[Dict[str, Any]] = None,
) -> SentinelNotificationEnvelope:
    symbol = decision["symbol"]
    direction = decision["direction"]
    severity = decision["severity"]
    summary = str(card.get("summary_short") or "")
    title = f"[{severity}][{direction}] {symbol}: {summary[:80]}"
    first_seen = _coerce_datetime(card.get("valid_from") or card.get("created_at") or card.get("updated_at"))
    return SentinelNotificationEnvelope(
        title=title,
        severity=severity,
        direction=direction,
        symbols=list(decision.get("symbols") or [symbol]),
        summary=summary,
        why_triggered=list(decision.get("why_triggered") or []),
        source_count=int(card.get("source_count") or 0),
        first_seen_at=first_seen,
        card_id=str(card.get("card_id") or ""),
        event_id=decision.get("event_id") or None,
        trace_id=str((graph_trace or {}).get("trace_id") or "") or None,
        graph_trace=graph_trace or {"status": "skipped"},
        links=_source_links(card),
        transmission_paths=_transmission_paths(card),
        diagnostics={
            "event_type": decision.get("event_type"),
            "evidence_grade": card.get("evidence_grade"),
            "mapping_confidence": card.get("mapping_confidence"),
            "signal_score": card.get("signal_score"),
        },
    )


def _build_heartbeat_envelope(
    *,
    run_id: str,
    current: datetime,
    universe: WatchedUniverse,
    source_result: Dict[str, Any],
    errors: List[Any],
    suppressed_by_cooldown: int,
    stale_card_suppressed: int,
) -> SentinelNotificationEnvelope:
    fetched = int(source_result.get("fetched_count") or 0)
    unseen = int(source_result.get("unseen_count") or 0)
    card_count = int(source_result.get("card_count") or 0)
    source_query_count = len(universe.source_queries)
    summary_parts = [
        f"StockAnalyser 新闻哨兵存活：本轮扫描 {card_count} 张新闻信号卡片，抓取 {fetched} 条，新增原始新闻 {unseen} 条。",
        "未产生新的市场触发通知。",
    ]
    if suppressed_by_cooldown:
        summary_parts.append(f"其中 {suppressed_by_cooldown} 条被冷却规则压制。")
    if stale_card_suppressed:
        summary_parts.append(f"另有 {stale_card_suppressed} 张旧卡片超过新鲜度窗口，未作为新告警发送。")
    if errors:
        summary_parts.append(f"本轮存在 {len(errors)} 个非阻断错误，已写入审计。")

    why = [
        "哨兵存活",
        "无新增触发",
        f"扫描卡片={card_count}",
        f"关注查询={source_query_count}",
    ]
    if suppressed_by_cooldown:
        why.append(f"冷却压制={suppressed_by_cooldown}")
    if stale_card_suppressed:
        why.append(f"旧卡压制={stale_card_suppressed}")

    return SentinelNotificationEnvelope(
        title=f"[heartbeat][neutral] StockAnalyser 新闻哨兵: {current.strftime('%H:%M')} 本轮无新增触发",
        severity="low",
        direction="neutral",
        symbols=["SENTINEL:HEARTBEAT"],
        summary="".join(summary_parts),
        why_triggered=why,
        source_count=fetched,
        first_seen_at=current,
        card_id=f"heartbeat:{run_id}",
        event_id=None,
        trace_id=None,
        graph_trace={"status": "skipped", "reason": "heartbeat"},
        links=[],
        transmission_paths=[],
        diagnostics={
            "event_type": "heartbeat",
            "run_id": run_id,
            "cards_scanned": card_count,
            "fetched_count": fetched,
            "unseen_count": unseen,
            "suppressed_by_cooldown": suppressed_by_cooldown,
            "stale_card_suppressed": stale_card_suppressed,
        },
    )


def _card_freshness_time(card: Dict[str, Any]) -> Optional[datetime]:
    if _is_portfolio_anysearch_card(card):
        return _portfolio_anysearch_published_time(card)
    for key in ("valid_from", "created_at", "updated_at"):
        parsed = _coerce_datetime(card.get(key))
        if parsed is not None:
            return parsed
    for item in card.get("source_chain") or []:
        if not isinstance(item, dict):
            continue
        for key in ("published_at", "created_at", "updated_at"):
            parsed = _coerce_datetime(item.get(key))
            if parsed is not None:
                return parsed
    return None


def _portfolio_anysearch_published_time(card: Dict[str, Any]) -> Optional[datetime]:
    for key in ("published_at", "event_time", "valid_from"):
        parsed = _coerce_datetime(card.get(key))
        if parsed is not None:
            return parsed
    for item in card.get("source_chain") or []:
        if not isinstance(item, dict):
            continue
        for key in ("published_at", "published_date", "event_time"):
            parsed = _coerce_datetime(item.get(key))
            if parsed is not None:
                return parsed
    for event in card.get("extracted_events") or []:
        if not isinstance(event, dict):
            continue
        for key in ("published_at", "event_time"):
            parsed = _coerce_datetime(event.get(key))
            if parsed is not None:
                return parsed
    return None


def _portfolio_anysearch_max_age_days(config: Any) -> int:
    return _bounded_int(
        getattr(config, "news_signal_portfolio_anysearch_max_age_days", 3),
        1,
        90,
        3,
    )


def _is_portfolio_anysearch_card(card: Dict[str, Any]) -> bool:
    diagnostics = card.get("diagnostics") if isinstance(card.get("diagnostics"), dict) else {}
    if str(diagnostics.get("source") or "") == "portfolio_anysearch":
        return True
    return any(
        isinstance(item, dict) and str(item.get("provider") or item.get("source") or "") == "portfolio_anysearch"
        for item in card.get("source_chain") or []
    )


def _merge_cards(left: List[Dict[str, Any]], right: List[Dict[str, Any]], *, limit: int) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for card in [*right, *left]:
        card_id = str(card.get("card_id") or "").strip()
        if not card_id or card_id in seen:
            continue
        seen.add(card_id)
        merged.append(card)
        if len(merged) >= max(1, int(limit or 50)):
            break
    return merged


def _recent_portfolio_anysearch_cards(
    service: Any,
    *,
    config: Any,
    now: datetime,
    limit: int,
) -> List[Dict[str, Any]]:
    try:
        from sqlalchemy import desc, select

        from src.repositories.news_signal_repo import NewsSignalRepository
        from src.storage import NewsSignalCard

        repo = getattr(service, "repo", None)
        if not isinstance(repo, NewsSignalRepository):
            return []
        interval_minutes = _bounded_int(
            getattr(config, "news_signal_portfolio_anysearch_interval_minutes", 300),
            60,
            1440,
            300,
        )
        sentinel_interval = _bounded_int(
            getattr(config, "news_event_sentinel_interval_minutes", 10),
            5,
            120,
            10,
        )
        lookback_minutes = min(1440, interval_minutes + sentinel_interval + 15)
        since = now.replace(tzinfo=None) - timedelta(minutes=lookback_minutes)
        source_since = now.replace(tzinfo=None) - timedelta(days=_portfolio_anysearch_max_age_days(config))
        with repo.db.get_session() as session:
            rows = session.execute(
                select(NewsSignalCard)
                .where(NewsSignalCard.status == "active")
                .where(NewsSignalCard.diagnostics_json.like("%portfolio_anysearch%"))
                .where(NewsSignalCard.updated_at >= since)
                .where(NewsSignalCard.valid_from >= source_since)
                .order_by(desc(NewsSignalCard.updated_at), desc(NewsSignalCard.signal_score))
                .limit(max(1, int(limit or 50)))
            ).scalars().all()
            return [repo.card_to_dict(row) for row in rows]
    except Exception as exc:
        logger.warning("recent portfolio AnySearch cards scan failed: %s", exc)
        return []


def _card_symbols(card: Dict[str, Any]) -> List[str]:
    symbols: List[str] = []
    for item in card.get("company_impacts") or []:
        if not isinstance(item, dict):
            continue
        symbol = _canonical_symbol(item.get("symbol") or item.get("code"))
        if symbol:
            symbols.append(symbol)
    return _unique(symbols)


def _card_direction(card: Dict[str, Any]) -> str:
    for item in card.get("company_impacts") or []:
        if isinstance(item, dict):
            direction = _normalize_direction(item.get("direction") or item.get("impact_direction"))
            if direction != "neutral":
                return direction
    event = _primary_event(card)
    direction = _normalize_direction(event.get("direction"))
    if direction != "neutral":
        return direction
    return _normalize_direction(card.get("news_tone"))


def _primary_event(card: Dict[str, Any]) -> Dict[str, Any]:
    events = card.get("extracted_events") if isinstance(card.get("extracted_events"), list) else []
    for event in events:
        if isinstance(event, dict):
            return event
    return {}


def _severity_for_card(card: Dict[str, Any], event_type: str) -> str:
    score = float(card.get("signal_score") or 0.0)
    evidence_grade = str(card.get("evidence_grade") or "").lower()
    mapping_confidence = float(card.get("mapping_confidence") or 0.0)
    if str(card.get("signal_layer") or "").lower() == "macro":
        if score >= 90:
            return "critical"
        if score >= 75:
            return "high"
        if score >= 50:
            return "mid"
        return "low"
    if event_type in _HIGH_RISK_EVENT_TYPES and evidence_grade in {"confirmed", "plausible"}:
        return "high" if score < 90 else "critical"
    if score >= 90 and mapping_confidence >= 0.7:
        return "critical"
    if score >= 75 and mapping_confidence >= 0.5:
        return "high"
    if score >= 50:
        return "mid"
    return "low"


def _transmission_paths(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    paths = card.get("transmission_paths") if isinstance(card.get("transmission_paths"), list) else []
    result: List[Dict[str, Any]] = []
    for item in paths:
        if not isinstance(item, dict):
            continue
        result.append(dict(item))
        if len(result) >= 5:
            break
    return result


def _feishu_template_for(envelope: SentinelNotificationEnvelope) -> str:
    severity = str(envelope.severity or "").lower()
    direction = str(envelope.direction or "").lower()
    if severity == "critical":
        return "red"
    if severity == "high":
        return "red" if direction == "negative" else "orange"
    if severity == "mid":
        return "orange" if direction == "negative" else "blue"
    return "grey"


def _format_transmission_path(item: Dict[str, Any]) -> str:
    path = str(item.get("path") or "").strip()
    mechanism = str(item.get("mechanism") or "").strip()
    target = str(item.get("target") or "").strip()
    parts: List[str] = []
    if path:
        parts.append(path)
    if mechanism:
        parts.append(f"机制={mechanism}")
    if target and target not in path:
        parts.append(f"目标={target}")
    return _truncate_text("；".join(parts), 220)


def _format_source_link(item: Dict[str, str]) -> str:
    title = _truncate_text(str(item.get("title") or item.get("url") or "source"), 80)
    url = str(item.get("url") or "").strip()
    if url:
        return f"[{title}]({url})"
    return title


def _format_graph_trace(trace: Dict[str, Any]) -> str:
    if not isinstance(trace, dict) or not trace:
        return ""
    status = str(trace.get("status") or "skipped")
    reason = str(trace.get("fallback_reason") or trace.get("reason") or "").strip()
    if status == "skipped" and reason in {"heartbeat", "trace_budget_exhausted"}:
        return ""

    edge_lines = _unique_nonempty(
        _format_graph_trace_edge(item)
        for item in (trace.get("edges") or [])
        if isinstance(item, dict)
    )[:3]
    episode_lines = _unique_nonempty(
        _format_graph_trace_episode(item)
        for item in (trace.get("episodes") or [])
        if isinstance(item, dict)
    )[:2]
    node_lines = _unique_nonempty(
        _format_graph_trace_node(item)
        for item in (trace.get("nodes") or [])
        if isinstance(item, dict)
    )[:2]

    source = _graph_trace_source_label(trace.get("source"))
    status_label = _graph_trace_status_label(status, source=source)
    counts = _graph_trace_counts(len(edge_lines), len(episode_lines), len(node_lines))
    lines = [f"状态：{status_label}{counts}"]

    if reason:
        lines.append(f"说明：{_graph_trace_reason_label(reason, trace)}")
    error = str(trace.get("graphiti_error") or trace.get("error") or "").strip()
    if error and status in {"failed", "degraded"}:
        lines.append(f"异常：{_truncate_text(error, 110)}")

    if edge_lines:
        lines.append("关联线索：\n" + "\n".join(f"- {item}" for item in edge_lines))

    if episode_lines:
        lines.append("相关事件：\n" + "\n".join(f"- {item}" for item in episode_lines))

    if node_lines:
        lines.append("相关主题：" + "、".join(node_lines))
    return "\n".join(lines)


def _format_graph_trace_edge(item: Dict[str, Any]) -> str:
    fact = _clean_graph_trace_text(item.get("fact") or item.get("rationale"))
    if fact:
        return _truncate_text(fact, 96)

    edge_type = _graph_relation_label(item.get("edge_type") or item.get("type") or item.get("name"))
    if not edge_type or edge_type in {"提及", "关联"}:
        return ""
    source = _clean_graph_trace_text(item.get("source") or item.get("source_name") or item.get("source_card_id"))
    target = _clean_graph_trace_text(item.get("target") or item.get("target_name") or item.get("target_id"))
    if source and target:
        return _truncate_text(f"{source} -> {edge_type} -> {target}", 96)
    return edge_type


def _format_graph_trace_episode(item: Dict[str, Any]) -> str:
    name = str(item.get("name") or "").strip()
    source_description = str(item.get("source_description") or "").strip()
    if name.startswith("trace:") or source_description == "agent_trace":
        return ""

    label = (
        item.get("summary_short")
        or item.get("analysis_summary")
        or item.get("title")
        or item.get("card_id")
        or _extract_graph_episode_summary(item.get("content"))
        or item.get("name")
    )
    label = _clean_graph_trace_text(label)
    return _truncate_text(label, 96)


def _format_graph_trace_node(item: Dict[str, Any]) -> str:
    attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    stock_name = _clean_graph_trace_text(attrs.get("stock_name"))
    code = _clean_graph_trace_text(attrs.get("code"))
    label = (
        stock_name
        or _clean_graph_trace_text(item.get("label") or item.get("name") or item.get("code") or item.get("id"))
    )
    if not label or _looks_like_graph_debug_id(label):
        return ""
    if code and code not in label:
        label = f"{label}({code})"

    type_label = _graph_node_type_label(item.get("type") or item.get("labels"))
    if type_label:
        return _truncate_text(f"{label}（{type_label}）", 56)
    return _truncate_text(label, 56)


def _graph_trace_source_label(value: Any) -> str:
    source = str(value or "").strip()
    if source == "relational_fallback":
        return "关系库兜底"
    if source:
        return "Graphiti" if source == "graphiti" else source
    return "Graphiti"


def _graph_trace_status_label(status: str, *, source: str) -> str:
    if status == "linked":
        return f"已关联 {source}"
    if status == "degraded":
        return f"{source}可用，结果已降级"
    if status == "empty":
        return f"{source} 未找到强关联"
    if status == "failed":
        return f"{source}查询失败"
    return "未查询"


def _graph_trace_counts(edge_count: int, episode_count: int, node_count: int) -> str:
    parts = []
    if edge_count:
        parts.append(f"{edge_count}条关系")
    if episode_count:
        parts.append(f"{episode_count}条事件")
    if node_count:
        parts.append(f"{node_count}个主题")
    return f"（命中{'、'.join(parts)}）" if parts else ""


def _graph_trace_reason_label(reason: str, trace: Dict[str, Any]) -> str:
    if reason == "filtered_weak_relevance":
        total = int(trace.get("filtered_out_count") or 0)
        suffix = f"，已过滤 {total} 条弱相关命中" if total else ""
        return f"图谱有召回结果，但与当前新闻主题重合度不足{suffix}。"
    if reason == "graphiti_disabled":
        return "Graphiti 当前不可用，已使用关系库兜底或跳过。"
    if reason == "graphiti_search_timeout":
        return "Graphiti 查询超时，已降级处理。"
    return _truncate_text(reason, 90)


def _filter_graph_trace_results(
    card: Dict[str, Any],
    decision: Dict[str, Any],
    *,
    edges: List[Dict[str, Any]],
    nodes: List[Dict[str, Any]],
    episodes: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], int]:
    anchors = _graph_trace_anchors(card, decision)
    if not anchors["strong"] and not anchors["context"]:
        return [], [], [], len(edges) + len(nodes) + len(episodes)

    kept_edges = [item for item in edges if _graph_trace_is_relevant(item, anchors)]
    kept_nodes = [item for item in nodes if _graph_trace_is_relevant(item, anchors)]
    kept_episodes = [item for item in episodes if _graph_trace_is_relevant(item, anchors)]
    filtered_out = (
        len(edges) + len(nodes) + len(episodes)
        - len(kept_edges) - len(kept_nodes) - len(kept_episodes)
    )
    return kept_edges, kept_nodes, kept_episodes, filtered_out


_GRAPH_TRACE_GENERIC_TERMS = {
    "财联社",
    "日电",
    "显示",
    "预计",
    "相关",
    "行业",
    "公司",
    "企业",
    "市场",
    "消息",
    "新闻",
    "频道",
    "报道",
    "提到",
    "增长",
    "上涨",
    "下跌",
    "价格",
    "成本",
    "订单",
    "突破",
    "同比",
    "同比增长",
    "上半",
    "半年",
    "上半年",
    "实时快讯",
    "亿元",
    "亿美元",
    "万亿",
    "亿",
    "技术",
    "变化",
    "确定",
    "盘面直播",
    "a股",
    "a股公告速递",
    "股公告速递",
    "公告速递",
    "科创板",
    "科创板最新动态",
    "最新动态",
}


def _graph_trace_anchors(card: Dict[str, Any], decision: Dict[str, Any]) -> Dict[str, set[str]]:
    strong: set[str] = set()
    strict: set[str] = set()
    context: set[str] = set()

    for value in decision.get("symbols") or []:
        symbol = str(value or "").strip()
        if symbol and not symbol.startswith(("SIGNAL:", "MACRO:")):
            _add_graph_strict_anchor(symbol, strong, strict)

    company_scoped = bool(card.get("company_impacts")) or str(card.get("signal_layer") or "").lower() == "company"
    summary = str(card.get("summary_short") or "")
    for term in _graph_trace_stock_terms(summary):
        _add_graph_strict_anchor(term, strong, strict)
    headline_entity = _graph_trace_headline_entity(summary)
    if headline_entity and (company_scoped or strict):
        _add_graph_strict_anchor(headline_entity, strong, strict)

    for key in ("primary_industries", "secondary_industries"):
        for value in card.get(key) or []:
            _add_graph_anchor(str(value or ""), strong)

    for value in card.get("explicit_entities") or []:
        if company_scoped:
            _add_graph_strict_anchor(str(value or ""), strong, strict)
        else:
            _add_graph_anchor(str(value or ""), strong)

    for company in card.get("company_impacts") or []:
        if not isinstance(company, dict):
            continue
        for key in ("symbol", "name", "code"):
            _add_graph_strict_anchor(str(company.get(key) or ""), strong, strict)

    text_parts = [summary]
    for path in card.get("transmission_paths") or []:
        if not isinstance(path, dict):
            continue
        for value in path.get("affected_symbols") or []:
            _add_graph_strict_anchor(str(value or ""), strong, strict)
        if company_scoped or strict:
            _add_graph_strict_anchor(str(path.get("target") or ""), strong, strict)
        for key in ("source", "target", "rationale"):
            text_parts.append(str(path.get(key) or ""))
        for snippet in path.get("evidence_snippets") or []:
            text_parts.append(str(snippet or ""))
        for step in path.get("chain_steps") or []:
            if isinstance(step, dict):
                text_parts.append(str(step.get("text") or ""))
    for term in _graph_trace_context_terms(" ".join(text_parts)):
        if term not in strong and term not in strict:
            context.add(term)
    return {"strong": strong, "strict": strict, "context": context}


def _add_graph_strict_anchor(value: str, strong: set[str], strict: set[str]) -> None:
    _add_graph_anchor(value, strong)
    _add_graph_anchor(value, strict)


def _add_graph_anchor(value: str, target: set[str]) -> None:
    text = _clean_graph_trace_text(value).lower()
    if not text or text in _GRAPH_TRACE_GENERIC_TERMS:
        return
    if len(text) >= 2:
        target.add(text)


def _graph_trace_context_terms(text: str) -> set[str]:
    normalized = str(text or "").lower()
    terms: set[str] = set()
    for token in re.findall(r"[a-z][a-z0-9._-]{2,}", normalized):
        if token and token not in _GRAPH_TRACE_GENERIC_TERMS:
            terms.add(token)
    for token in re.findall(r"(?<![\d.])[0-9]+(?:\.[0-9]+)?(?:万亿|亿元|亿美元|gwh|亿|%|pct)", normalized):
        if token and token not in _GRAPH_TRACE_GENERIC_TERMS:
            terms.add(token)
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        for term in _graph_trace_chinese_anchor_terms(chunk):
            if term not in _GRAPH_TRACE_GENERIC_TERMS:
                terms.add(term)
    return terms


def _graph_trace_chinese_anchor_terms(chunk: str) -> set[str]:
    text = str(chunk or "").strip()
    if not text:
        return set()

    terms: set[str] = set()
    cleaned = text
    for prefix in ("财联社", "上半年", "年上半年", "其中"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    for suffix in ("发布", "显示", "情况", "同比增长"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
    if 2 <= len(cleaned) <= 8:
        terms.add(cleaned)

    # Extract complete domain phrases. Sliding character n-grams are too noisy
    # for Graphiti trace filtering because generic overlaps like "同比增长" or
    # "上半年" connect unrelated finance headlines.
    domain_nouns = (
        "财政部",
        "财政收支",
        "证券交易印花税",
        "交易印花税",
        "印花税",
        "沪深两市",
        "成交额",
        "储能系统",
        "储能",
        "半导体",
        "算力",
        "晶圆代工",
        "机器人",
        "新能源",
        "关税",
        "贸易协定",
        "供应链",
    )
    for noun in domain_nouns:
        if noun in text:
            terms.add(noun)
    return terms


def _graph_trace_stock_terms(text: str) -> set[str]:
    terms: set[str] = set()
    normalized = str(text or "")
    for name, code in re.findall(
        r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9·]{1,24})\((\d{6})(?:\.[A-Za-z]+)?\)",
        normalized,
    ):
        terms.add(name)
        terms.add(code)
    for code in re.findall(r"(?<!\d)(?:SH|SZ|BJ)?(\d{6})(?:\.(?:SH|SZ|BJ))?(?!\d)", normalized, flags=re.IGNORECASE):
        terms.add(code)
    return terms


def _graph_trace_headline_entity(text: str) -> str:
    match = re.match(r"\s*【([^：:】]{2,24})[：:】]", str(text or ""))
    if not match:
        return ""
    return match.group(1).strip()


def _graph_trace_is_relevant(item: Dict[str, Any], anchors: Dict[str, set[str]]) -> bool:
    name = str(item.get("name") or "").strip()
    source_description = str(item.get("source_description") or "").strip()
    if name.startswith("trace:") or source_description == "agent_trace":
        return False
    text = _graph_trace_item_text(item).lower()
    if not text:
        return False
    strict_terms = anchors.get("strict") or set()
    strict_hits = sum(1 for term in strict_terms if term and term in text)
    if strict_terms:
        return bool(strict_hits)
    strong_hits = sum(1 for term in anchors["strong"] if term and term in text)
    if strong_hits:
        return True
    context_hits = sum(1 for term in anchors["context"] if len(term) >= 2 and term in text)
    return context_hits >= 2


def _graph_trace_item_text(item: Dict[str, Any]) -> str:
    values: List[str] = []
    for key in ("fact", "rationale", "summary_short", "analysis_summary", "title", "name", "content", "summary", "card_id"):
        value = item.get(key)
        if value:
            values.append(str(value))
    attrs = item.get("attributes")
    if isinstance(attrs, dict):
        for key in ("stock_name", "code", "market", "industry", "sector"):
            value = attrs.get(key)
            if value:
                values.append(str(value))
    return " ".join(values)


def _graph_relation_label(value: Any) -> str:
    raw = str(value or "").strip()
    normalized = raw.upper()
    mapping = {
        "MENTIONS": "提及",
        "HAS_ORDER": "订单关联",
        "HAS_ENTITY": "实体关联",
        "NEWS_SIGNAL_SEMANTIC_SIMILARITY": "语义相似",
        "NEWS_SIGNAL_EVENT_CLUE": "事件线索",
        "NEWS_SIGNAL_TYPED_RELATION": "传导关系",
        "IMPACTS_INDUSTRY": "影响行业",
        "IMPACTS_COMPANY": "影响公司",
    }
    return mapping.get(normalized, raw.replace("_", " ").strip())


def _graph_node_type_label(value: Any) -> str:
    values: List[str] = []
    if isinstance(value, (list, tuple, set)):
        values = [str(item) for item in value if str(item).strip()]
    elif value:
        values = [str(value)]
    values = [item for item in values if item and item != "Entity"]
    mapping = {
        "Stock": "股票",
        "MarketEvent": "事件",
        "ImpactVariable": "影响变量",
        "NewsSignalCard": "新闻卡",
        "NewsSignalTarget": "主题",
        "Community": "社区",
    }
    labels = [mapping.get(item, item) for item in values]
    return "、".join(labels[:2])


def _extract_graph_episode_summary(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            for key in ("summary_short", "title", "event_id"):
                candidate = _clean_graph_trace_text(payload.get(key))
                if candidate and not _looks_like_graph_debug_id(candidate):
                    return candidate
            event_payload = payload.get("event_payload")
            if isinstance(event_payload, dict):
                candidate = _clean_graph_trace_text(event_payload.get("summary") or event_payload.get("title"))
                if candidate:
                    return candidate
        return ""
    match = re.search(r"##\s*新闻摘要\s*\n+([^\n#]+)", text)
    if match:
        return _clean_graph_trace_text(match.group(1))
    for line in text.splitlines():
        cleaned = _clean_graph_trace_text(line.lstrip("#- "))
        if cleaned and not cleaned.startswith(("卡片 ID:", "信号日期:", "来源:", "链接:")):
            return cleaned
    return ""


def _clean_graph_trace_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    if _looks_like_graph_debug_id(text):
        return ""
    return text


def _looks_like_graph_debug_id(value: str) -> bool:
    text = str(value or "").strip()
    return bool(
        text.startswith(("trace:", "news_signal:trace:"))
        or re.fullmatch(r"[0-9a-f]{8,}(-[0-9a-f]{4,}){2,}", text, re.IGNORECASE)
        or re.fullmatch(r"[0-9a-f]{32,}", text, re.IGNORECASE)
    )


def _unique_nonempty(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _truncate_text(value: Any, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


def _source_links(card: Dict[str, Any]) -> List[Dict[str, str]]:
    links: List[Dict[str, str]] = []
    for item in card.get("source_chain") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or item.get("source") or url or "source").strip()
        if not url and not title:
            continue
        links.append({"title": title[:120], "url": url})
        if len(links) >= 3:
            break
    return links


def _graphiti_trace_query(card: Dict[str, Any], decision: Dict[str, Any]) -> str:
    parts: List[str] = []
    summary = str(card.get("summary_short") or "").strip()
    if summary:
        parts.append(summary)
    for symbol in decision.get("symbols") or []:
        value = str(symbol or "").strip()
        if value and not value.startswith("SIGNAL:") and not value.startswith("MACRO:"):
            parts.append(value)
    for value in card.get("primary_industries") or []:
        if str(value or "").strip():
            parts.append(str(value).strip())
    event_type = str(decision.get("event_type") or "").strip()
    if event_type and event_type != "unknown":
        parts.append(event_type)
    for company in card.get("company_impacts") or []:
        if not isinstance(company, dict):
            continue
        for key in ("symbol", "name"):
            value = str(company.get(key) or "").strip()
            if value:
                parts.append(value)
    return " ".join(_unique([item for item in parts if item]))[:500]


def _canonical_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalize_direction(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in _NEGATIVE_TONES:
        return "negative"
    if text in _POSITIVE_TONES:
        return "positive"
    return "neutral"


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _is_in_active_windows(now: datetime, windows: str) -> bool:
    text = str(windows or "").strip()
    if not text:
        return True
    minute_of_day = now.hour * 60 + now.minute
    parsed_any = False
    for window in text.split(","):
        part = window.strip()
        if not part or "-" not in part:
            continue
        start_text, end_text = [item.strip() for item in part.split("-", 1)]
        start = _parse_hhmm(start_text)
        end = _parse_hhmm(end_text)
        if start is None or end is None:
            continue
        parsed_any = True
        if start <= end:
            if start <= minute_of_day <= end:
                return True
        else:
            if minute_of_day >= start or minute_of_day <= end:
                return True
    return True if not parsed_any else False


def _parse_hhmm(value: str) -> Optional[int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (ValueError, AttributeError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _stable_id(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


def _unique(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _bounded_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))
