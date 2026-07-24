# -*- coding: utf-8 -*-
"""News signal card service.

The first version is deliberately deterministic: it can build cards from
existing news tools and the shared concept mapping without requiring LLM,
embedding or Graphiti to be available.
"""

from __future__ import annotations

import hashlib
import asyncio
import html
import json
import logging
import math
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.config import (
    extra_litellm_params,
    get_api_keys_for_model,
    get_config,
    get_configured_llm_models,
    normalize_litellm_temperature,
)
from src.agent.evidence.schemas import (
    DataQuality,
    EvidenceCard,
    EvidenceExpiry,
    EvidenceImpact,
    EvidenceSignal,
    StockRef,
)
from src.core.trading_calendar import get_effective_trading_date
from src.repositories.news_signal_repo import NewsSignalRepository
from src.repositories.graphiti_outbox_repo import GraphitiOutboxRepository
from src.services.graphiti.semantic_thresholds import resolve_semantic_threshold


NEWS_SIGNAL_SCHEMA_VERSION = "news_signal_card.v1"
SEMANTIC_EDGE_TOP_K_PER_CARD = 4
SEMANTIC_EDGE_MIN_QUALITY = 45.0
NEWS_EVENT_LLM_DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
NEWS_EVENT_LLM_EVENT_TYPES = {
    "价格/供需",
    "大客户/订单",
    "技术突破",
    "供应链/替代",
    "政策/宏观",
    "业绩验证",
    "产能变化",
    "其他",
}
NEWS_EVENT_LLM_DIRECTIONS = {"benefit", "harm", "neutral", "uncertain"}
NEWS_EVENT_LLM_VERIFICATION_STATUSES = {"source_verified", "source_only", "unverified"}
CONCEPT_MAPPING_PATH = (
    Path(__file__).resolve().parents[1]
    / "agent"
    / "candidate_experts_v2"
    / "resources"
    / "news_theme_daily"
    / "concept_mapping.json"
)

POSITIVE_TERMS = (
    "利好",
    "受益",
    "增长",
    "突破",
    "涨价",
    "降准",
    "降息",
    "净投放",
    "流动性充裕",
    "扩产",
    "中标",
    "订单",
    "国产替代",
    "供给收缩",
    "需求旺盛",
    "政策支持",
    "批量供货",
    "量产供货",
)
NEGATIVE_TERMS = (
    "利空",
    "下滑",
    "亏损",
    "处罚",
    "调查",
    "减产",
    "加息",
    "缩表",
    "净回笼",
    "流动性收紧",
    "通胀超预期",
    "禁令",
    "制裁",
    "澄清",
    "风险",
    "供给过剩",
    "需求疲软",
)
LONG_TERMS = ("规划", "长期", "产业趋势", "战略", "国产替代", "技术路线", "供给格局")
MEDIUM_TERMS = ("政策", "订单", "产能", "涨价", "需求", "景气", "验证", "客户")
TRUE_POSITIVE_TERMS = (
    "国外大厂",
    "海外大厂",
    "国际大厂",
    "英伟达",
    "nvidia",
    "苹果",
    "apple",
    "特斯拉",
    "tesla",
    "微软",
    "microsoft",
    "亚马逊",
    "amazon",
    "谷歌",
    "google",
    "meta",
    "台积电",
    "tsmc",
    "三星",
    "samsung",
    "批量供货",
    "量产供货",
    "批量交付",
    "规模化供货",
    "增持",
    "股份回购",
    "回购股份",
    "回购公司股份",
)
WEAK_POSITIVE_TERMS = (
    "框架协议",
    "意向协议",
    "合作备忘录",
    "战略合作协议",
    "战略合作",
    "不具约束力",
    "可撤销",
    "新增概念",
    "概念股",
    "概念业务",
    "开展研究",
    "相关研究",
    "研发",
    "布局",
    "正布局",
    "正在布局",
    "送样",
    "正送样",
    "样品",
    "客户验证",
)
MACRO_TERMS = (
    "非农",
    "就业数据",
    "失业率",
    "cpi",
    "ppi",
    "pmi",
    "gdp",
    "通胀",
    "美联储",
    "fomc",
    "加息",
    "降息",
    "利率",
    "国债收益率",
    "美元指数",
    "人民币汇率",
    "央行",
    "人民银行",
    "公开市场",
    "逆回购",
    "mlf",
    "lpr",
    "降准",
    "社融",
    "m2",
    "流动性",
    "净投放",
    "净回笼",
    "财政政策",
    "货币政策",
    "油价",
    "原油",
    "大宗商品",
)
ASCII_MACRO_TERMS = {"ADP", "CPI", "PPI", "PMI", "GDP", "PCE", "FOMC", "MLF", "LPR", "M2"}
CENTRAL_BANK_ENTITY_TERMS = {"央行", "人民银行"}
CENTRAL_BANK_OPERATION_TERMS = {
    "公开市场",
    "逆回购",
    "正回购",
    "MLF",
    "LPR",
    "降准",
    "降息",
    "加息",
    "存款准备金率",
    "流动性",
    "净投放",
    "净回笼",
    "货币政策",
}
MARKETING_TERMS = (
    "盘中宝",
    "电报解读",
    "这家公司",
    "相关公司",
    "VIP",
    "付费",
    "订阅",
    "研报",
)
GENERIC_COMPANY_TEASER_TERMS = (
    "这家公司",
    "这家公司的",
    "这家企业",
    "该公司",
    "相关公司",
    "公司上半年",
    "公司净利",
)
GENERIC_RELATION_THEMES = {
    "实时快讯",
    "实时消息",
    "市场快讯",
    "市场资讯",
    "财经新闻",
    "行业动态",
    "其他",
    "未分类",
}
SIGNAL_TERMS = tuple(dict.fromkeys(POSITIVE_TERMS + NEGATIVE_TERMS + LONG_TERMS + MEDIUM_TERMS + MACRO_TERMS))
logger = logging.getLogger(__name__)


class NewsEventLLMExtractor:
    """Optional lightweight JSON extractor for news event facts."""

    def __init__(self, config: Optional[Any] = None) -> None:
        self.config = config or get_config()

    def extract(
        self,
        context: Dict[str, Any],
        fallback_events: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        config = self.config
        mode = str(getattr(config, "news_event_extractor_mode", "fallback") or "fallback").strip().lower()
        if mode not in {"fallback", "llm", "auto"}:
            return [], {
                "status": "disabled",
                "reason": "invalid_mode",
                "mode": mode,
            }
        if mode == "fallback":
            return [], {"status": "disabled", "mode": mode}

        model = self._resolve_model()
        if not model:
            return [], {"status": "disabled", "mode": mode, "reason": "no_model"}
        if mode == "auto" and not self._model_has_runtime(model):
            return [], {
                "status": "disabled",
                "mode": mode,
                "model": model,
                "reason": "model_runtime_unavailable",
            }

        try:
            content, model_used, usage = self._call_llm(model, context)
            parsed = _parse_json_object(content)
            events = parsed.get("events") if isinstance(parsed, dict) else None
            if not isinstance(events, list):
                return [], {
                    "status": "failed",
                    "mode": mode,
                    "model": model_used,
                    "reason": "missing_events_array",
                    "raw_preview": _compact_text(content, 240),
                    "usage": usage,
                }
            normalized = self._normalize_events(
                events,
                context=context,
                fallback_events=fallback_events,
                model_used=model_used,
                usage=usage,
            )
            if not normalized:
                return [], {
                    "status": "empty",
                    "mode": mode,
                    "model": model_used,
                    "usage": usage,
                }
            return normalized, {
                "status": "ok",
                "mode": mode,
                "model": model_used,
                "usage": usage,
                "event_count": len(normalized),
            }
        except Exception as exc:
            logger.warning("News event LLM extraction failed: %s", exc)
            return [], {
                "status": "failed",
                "mode": mode,
                "model": model,
                "reason": exc.__class__.__name__,
                "message": _compact_text(str(exc), 240),
            }

    def _resolve_model(self) -> str:
        model = str(getattr(self.config, "news_event_extractor_model", "") or "").strip()
        if model:
            if "/" not in model and model.startswith("deepseek-"):
                return f"deepseek/{model}"
            return model
        if getattr(self.config, "deepseek_api_keys", None):
            return NEWS_EVENT_LLM_DEFAULT_MODEL
        return str(getattr(self.config, "litellm_model", "") or "").strip()

    def _model_has_runtime(self, model: str) -> bool:
        configured_models = set(get_configured_llm_models(getattr(self.config, "llm_model_list", []) or []))
        if model in configured_models:
            return True
        if get_api_keys_for_model(model, self.config):
            return True
        provider = model.split("/", 1)[0].lower() if "/" in model else ""
        if provider == "ollama":
            return True
        return provider not in {"gemini", "vertex_ai", "anthropic", "openai", "deepseek"}

    def _call_llm(self, model: str, context: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
        try:
            import litellm  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on optional runtime packaging
            raise RuntimeError(f"litellm unavailable: {exc}") from exc

        messages = [
            {"role": "system", "content": _news_event_extractor_system_prompt()},
            {"role": "user", "content": _news_event_extractor_user_prompt(context)},
        ]
        temperature = normalize_litellm_temperature(
            model,
            getattr(self.config, "news_event_extractor_temperature", 0.0),
            default=0.0,
            model_list=getattr(self.config, "llm_model_list", []) or [],
        )
        call_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": int(getattr(self.config, "news_event_extractor_max_tokens", 900) or 900),
            "timeout": int(getattr(self.config, "news_event_extractor_timeout_seconds", 12) or 12),
            "response_format": {"type": "json_object"},
        }
        response = self._completion(litellm, model, call_kwargs)
        content = _extract_litellm_content(response)
        return content, model, _normalize_litellm_usage(getattr(response, "usage", None))

    def _completion(self, litellm_module: Any, model: str, call_kwargs: Dict[str, Any]) -> Any:
        configured_models = set(get_configured_llm_models(getattr(self.config, "llm_model_list", []) or []))
        if getattr(self.config, "llm_model_list", None) and model in configured_models:
            router = litellm_module.Router(
                model_list=getattr(self.config, "llm_model_list", []) or [],
                routing_strategy="simple-shuffle",
                num_retries=1,
            )
            try:
                return router.completion(**call_kwargs)
            except Exception as exc:
                if "response_format" not in str(exc):
                    raise
                retry_kwargs = dict(call_kwargs)
                retry_kwargs.pop("response_format", None)
                return router.completion(**retry_kwargs)

        direct_kwargs = dict(call_kwargs)
        keys = get_api_keys_for_model(model, self.config)
        if keys:
            direct_kwargs["api_key"] = keys[0]
        direct_kwargs.update(extra_litellm_params(model, self.config))
        try:
            return litellm_module.completion(**direct_kwargs)
        except Exception as exc:
            if "response_format" not in str(exc):
                raise
            direct_kwargs.pop("response_format", None)
            return litellm_module.completion(**direct_kwargs)

    def _normalize_events(
        self,
        events: List[Any],
        *,
        context: Dict[str, Any],
        fallback_events: List[Dict[str, Any]],
        model_used: str,
        usage: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        fallback = fallback_events[0] if fallback_events else {}
        raw = context.get("raw") if isinstance(context.get("raw"), dict) else {}
        normalized: List[Dict[str, Any]] = []
        for index, item in enumerate(events[:3]):
            if not isinstance(item, dict):
                continue
            evidence = _compact_text(item.get("evidence_sentence") or fallback.get("evidence_sentence") or context.get("text"), 220)
            if not evidence:
                continue
            event_type = str(item.get("event_type") or fallback.get("event_type") or "其他").strip()
            if event_type not in NEWS_EVENT_LLM_EVENT_TYPES:
                event_type = _event_category_for_text(evidence)
            direction = str(item.get("direction") or fallback.get("direction") or _direction_from_tone(str(context.get("tone") or ""))).strip()
            if direction not in NEWS_EVENT_LLM_DIRECTIONS:
                direction = _direction_from_tone(str(context.get("tone") or ""))
            verification_status = str(item.get("verification_status") or fallback.get("verification_status") or "source_only").strip()
            if verification_status not in NEWS_EVENT_LLM_VERIFICATION_STATUSES:
                verification_status = "source_only"
            confidence = _bounded_float(item.get("confidence"), default=float(fallback.get("confidence") or 0.65), minimum=0.0, maximum=0.98)
            trigger = _compact_text(item.get("trigger") or fallback.get("trigger") or _event_trigger_for_text(event_type, evidence), 80)
            subject = _compact_text(item.get("subject") or fallback.get("subject") or context.get("primary_theme"), 100)
            obj = _compact_text(item.get("object") or fallback.get("object"), 140)
            metric_value = _compact_text(item.get("metric_value") or fallback.get("metric_value") or _metric_value_for_text(evidence), 80)
            entity_links = _normalize_llm_entity_links(item.get("entity_links"), fallback.get("entity_links") or [])
            event_time = raw.get("published_at") if isinstance(raw.get("published_at"), datetime) else _parse_datetime(raw.get("published_at"))
            event_id = _stable_id(
                "event",
                raw.get("episode_id"),
                context.get("card_id"),
                "llm",
                index,
                event_type,
                trigger,
                evidence,
            )
            normalized.append(
                {
                    "event_id": event_id,
                    "raw_episode_id": str(raw.get("episode_id") or ""),
                    "card_id": str(context.get("card_id") or ""),
                    "signal_date": raw.get("signal_date"),
                    "event_time": event_time,
                    "event_type": event_type,
                    "trigger": trigger,
                    "subject": subject,
                    "object": obj,
                    "direction": direction,
                    "metric_value": metric_value,
                    "evidence_sentence": evidence,
                    "source_url": str(raw.get("url") or ""),
                    "source": str(raw.get("source") or ""),
                    "extractor": f"llm_json:{model_used}",
                    "confidence": round(confidence, 3),
                    "verification_status": verification_status,
                    "verification_sources": fallback.get("verification_sources") or _event_verification_sources(raw),
                    "entity_links": entity_links,
                    "diagnostics": {
                        "schema_version": "news_extracted_event.v1",
                        "extractor_role": "primary_llm",
                        "llm_extraction": {
                            "status": "ok",
                            "model": model_used,
                            "usage": usage,
                        },
                        "fallback_event_id": fallback.get("event_id"),
                        "source_quality_score": _float_or_none(raw.get("quality_score")),
                        "mapping_confidence": context.get("mapping_confidence"),
                    },
                    "status": "active",
                }
            )
        return normalized


class NewsSignalService:
    """Application service for news signal cards."""

    def __init__(
        self,
        repo: Optional[NewsSignalRepository] = None,
        event_llm_extractor: Optional[Any] = None,
        outbox_repo: Optional[GraphitiOutboxRepository] = None,
    ) -> None:
        self.repo = repo or NewsSignalRepository()
        self.outbox_repo = outbox_repo or GraphitiOutboxRepository(self.repo.db)
        self._concept_mapping = _load_concept_mapping()
        self._event_llm_extractor = event_llm_extractor if event_llm_extractor is not None else NewsEventLLMExtractor()

    def rebuild(
        self,
        *,
        target_date: str = "",
        include_cjzc: bool = True,
        include_cls: bool = True,
        include_xueqiu: bool = True,
        include_macro_finance: bool = True,
        cls_limit: int = 50,
        xueqiu_limit: int = 30,
        macro_finance_limit: int = 30,
        sync_graphiti: bool = False,
        include_semantic_edges: bool = False,
    ) -> Dict[str, Any]:
        """Rebuild first-version cards from existing news sources."""

        target = _resolve_target_date(target_date)
        raw_payloads: List[Dict[str, Any]] = []
        card_payloads: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        if include_cjzc:
            source_result, source_error = self._fetch_cjzc(target)
            if source_error:
                errors.append(source_error)
            if source_result:
                raw, cards = self._build_from_cjzc(source_result, target)
                raw_payloads.extend(raw)
                card_payloads.extend(cards)

        if include_cls:
            source_result, source_error = self._fetch_cls(limit=cls_limit)
            if source_error:
                errors.append(source_error)
            if source_result:
                raw, cards = self._build_from_cls(source_result)
                raw_payloads.extend(raw)
                card_payloads.extend(cards)

        if include_xueqiu:
            source_result, source_error = self._fetch_xueqiu(limit=xueqiu_limit)
            if source_error:
                errors.append(source_error)
            if source_result:
                raw, cards = self._build_from_xueqiu(source_result)
                raw_payloads.extend(raw)
                card_payloads.extend(cards)

        if include_macro_finance:
            source_result, source_error = self._fetch_macro_finance(limit=macro_finance_limit)
            if source_error:
                errors.append(source_error)
            if source_result:
                raw, cards = self._build_from_macro_finance(source_result)
                raw_payloads.extend(raw)
                card_payloads.extend(cards)

        saved_raw = self.repo.upsert_raw_episodes(raw_payloads)
        saved_events = self.repo.upsert_extracted_events(_collect_extracted_events(card_payloads))
        saved_cards = self.repo.upsert_cards(card_payloads)
        edge_sync = (
            self.rebuild_edges(
                signal_date=target.isoformat(),
                include_semantic=include_semantic_edges,
            )
            if saved_cards
            else {"status": "skipped", "reason": "no_saved_cards"}
        )
        cluster_sync = (
            self.reconcile_same_event_clusters(signal_date=target.isoformat(), limit=500)
            if saved_cards
            else {"status": "skipped", "reason": "no_saved_cards"}
        )
        active_saved_cards = [
            detail
            for card in saved_cards
            if (detail := self.repo.get_card(str(card.get("card_id") or "")))
            and detail.get("status") == "active"
        ]
        outbox_enqueued = self._enqueue_graphiti_jobs(
            active_saved_cards,
            signal_dates=[target.isoformat()] if saved_cards else [],
        )
        graph_sync = (
            self.sync_graphiti(
                card_ids=[str(card.get("card_id") or "") for card in saved_cards if card.get("card_id")],
                signal_date=target.isoformat(),
                include_semantic_edges=include_semantic_edges,
            )
            if sync_graphiti
            else {"status": "skipped", "reason": "disabled_by_request"}
        )

        return {
            "status": "ok" if saved_cards else ("partial" if saved_raw or errors else "empty"),
            "target_date": target.isoformat(),
            "raw_episodes_upserted": len(saved_raw),
            "events_upserted": len(saved_events),
            "cards_upserted": len(saved_cards),
            "edge_sync": edge_sync,
            "cluster_sync": cluster_sync,
            "graph_sync": graph_sync,
            "outbox_enqueued": outbox_enqueued,
            "errors": errors,
            "cards": saved_cards,
        }

    def ingest_cls_incremental(self, *, limit: int = 50) -> Dict[str, Any]:
        """Persist only unseen CLS feed episodes and rebuild affected relations."""

        payload, source_error = self._fetch_cls(limit=max(1, min(int(limit or 50), 50)))
        if source_error or not payload:
            return {
                "status": "failed",
                "source": "cls_telegraph",
                "new_raw_episodes": 0,
                "events_upserted": 0,
                "cards_upserted": 0,
                "cursor": self.repo.latest_raw_episode_cursor("cls_telegraph"),
                "errors": [source_error or {"source": "cls_telegraph", "error": "empty payload"}],
            }

        raw_payloads, card_payloads = self._build_from_cls(payload)
        existing_ids = self.repo.existing_raw_episode_ids(
            item.get("episode_id") for item in raw_payloads
        )
        new_raw_payloads = [
            item for item in raw_payloads
            if str(item.get("episode_id") or "") not in existing_ids
        ]
        new_episode_ids = {
            str(item.get("episode_id") or "")
            for item in new_raw_payloads
            if item.get("episode_id")
        }
        new_card_payloads = [
            card for card in card_payloads
            if new_episode_ids.intersection(
                str(value or "") for value in card.get("raw_episode_ids") or []
            )
        ]

        saved_raw = self.repo.upsert_raw_episodes(new_raw_payloads)
        saved_events = self.repo.upsert_extracted_events(
            _collect_extracted_events(new_card_payloads)
        )
        saved_cards = self.repo.upsert_cards(new_card_payloads)
        affected_dates = sorted(
            {
                str(card.get("signal_date") or "")
                for card in saved_cards
                if card.get("signal_date")
            }
        )
        edge_sync = [
            self.rebuild_edges(signal_date=signal_date, limit=500)
            for signal_date in affected_dates
        ]
        cluster_sync = [
            self.reconcile_same_event_clusters(signal_date=signal_date, limit=500)
            for signal_date in affected_dates
        ]
        active_saved_cards = [
            detail
            for card in saved_cards
            if (detail := self.repo.get_card(str(card.get("card_id") or "")))
            and detail.get("status") == "active"
        ]
        outbox_enqueued = self._enqueue_graphiti_jobs(
            active_saved_cards,
            signal_dates=affected_dates,
        )
        return {
            "status": "ok" if saved_raw else "empty",
            "source": "cls_telegraph",
            "fetched_items": len(raw_payloads),
            "new_raw_episodes": len(saved_raw),
            "events_upserted": len(saved_events),
            "cards_upserted": len(saved_cards),
            "affected_dates": affected_dates,
            "edge_sync": edge_sync,
            "cluster_sync": cluster_sync,
            "outbox_enqueued": outbox_enqueued,
            "cursor": self.repo.latest_raw_episode_cursor("cls_telegraph"),
            "errors": [],
        }

    def ingest_portfolio_anysearch_news(
        self,
        *,
        search_client: Optional[Any] = None,
        name_resolver: Optional[Any] = None,
        max_results_per_stock: int = 5,
        max_age_days: int = 3,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Search actionable news for real portfolio holdings and persist cards.

        This produces message-surface ``portfolio_anysearch`` cards only. It does
        not run stock analysis, market review, or report notification flows.
        """

        current = (now or datetime.now()).replace(tzinfo=None)
        holdings = _load_active_portfolio_holdings()
        if not holdings:
            return {
                "status": "empty",
                "source": "portfolio_anysearch",
                "holding_count": 0,
                "searched_holdings": 0,
                "fetched_items": 0,
                "accepted_items": 0,
                "filtered_items": 0,
                "new_raw_episodes": 0,
                "events_upserted": 0,
                "cards_upserted": 0,
                "affected_dates": [],
                "outbox_enqueued": 0,
                "errors": [],
                "cards": [],
            }

        client = search_client or _default_portfolio_anysearch_client()
        if client is None:
            return {
                "status": "failed",
                "source": "portfolio_anysearch",
                "holding_count": len(holdings),
                "searched_holdings": 0,
                "fetched_items": 0,
                "accepted_items": 0,
                "filtered_items": 0,
                "new_raw_episodes": 0,
                "events_upserted": 0,
                "cards_upserted": 0,
                "affected_dates": [],
                "outbox_enqueued": 0,
                "errors": [{"source": "portfolio_anysearch", "error": "ANYSEARCH_API_KEY not configured"}],
                "cards": [],
            }

        max_results = max(1, min(int(max_results_per_stock or 5), 10))
        max_age = max(1, min(int(max_age_days or 3), 90))
        resolver = name_resolver or _PortfolioStockNameResolver()
        payload_results: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        filtered_items = 0
        fetched_items = 0
        searched_holdings = 0

        for holding in holdings:
            symbol = str(holding.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            market = str(holding.get("market") or "cn").strip().lower() or "cn"
            try:
                name = str(resolver(symbol, market) or "").strip()
            except Exception as exc:
                name = ""
                errors.append({"symbol": symbol, "source": "name_resolver", "error": str(exc)})
            if not name:
                name = symbol
            query = _portfolio_anysearch_query(symbol=symbol, name=name, market=market)
            try:
                response = _run_portfolio_news_search(
                    client,
                    query=query,
                    max_results=max_results,
                    max_age_days=max_age,
                )
            except Exception as exc:
                errors.append({"symbol": symbol, "query": query, "source": "anysearch", "error": str(exc)})
                continue

            searched_holdings += 1
            if not getattr(response, "success", False):
                errors.append(
                    {
                        "symbol": symbol,
                        "query": query,
                        "source": "anysearch",
                        "error": str(getattr(response, "error_message", None) or "search failed"),
                    }
                )
                continue

            results = list(getattr(response, "results", []) or [])[:max_results]
            fetched_items += len(results)
            for rank, result in enumerate(results, start=1):
                gate = _portfolio_anysearch_gate(
                    result,
                    symbol=symbol,
                    name=name,
                    now=current,
                    max_age_days=max_age,
                )
                if not gate["accepted"]:
                    filtered_items += 1
                    continue
                payload_results.append(
                    _portfolio_anysearch_dailynews_item(
                        result,
                        symbol=symbol,
                        name=name,
                        market=market,
                        holding=holding,
                        query=query,
                        rank=rank,
                        provider=str(getattr(response, "provider", "") or "AnySearch"),
                        gate=gate,
                        now=current,
                    )
                )

        raw_payloads, card_payloads = self._build_from_dailynews(
            {
                "status": "ok",
                "provider": "AnySearch",
                "results": payload_results,
                "source_chain": [{"provider": "AnySearch", "result": "ok", "scope": "portfolio_holdings"}],
                "errors": errors,
            },
            source="portfolio_anysearch",
            raw_prefix="portfolio_anysearch",
            default_title="持仓消息面",
        )
        existing_ids = self.repo.existing_raw_episode_ids(
            item.get("episode_id") for item in raw_payloads
        )
        new_raw_payloads = [
            item for item in raw_payloads
            if str(item.get("episode_id") or "") not in existing_ids
        ]
        new_episode_ids = {
            str(item.get("episode_id") or "")
            for item in new_raw_payloads
            if item.get("episode_id")
        }
        new_card_payloads = [
            card for card in card_payloads
            if new_episode_ids.intersection(
                str(value or "") for value in card.get("raw_episode_ids") or []
            )
        ]

        saved_raw = self.repo.upsert_raw_episodes(new_raw_payloads)
        saved_events = self.repo.upsert_extracted_events(
            _collect_extracted_events(new_card_payloads)
        )
        saved_cards = self.repo.upsert_cards(new_card_payloads)
        affected_dates = sorted(
            {
                str(card.get("signal_date") or "")
                for card in saved_cards
                if card.get("signal_date")
            }
        )
        edge_sync = [
            self.rebuild_edges(signal_date=signal_date, limit=500)
            for signal_date in affected_dates
        ]
        cluster_sync = [
            self.reconcile_same_event_clusters(signal_date=signal_date, limit=500)
            for signal_date in affected_dates
        ]
        active_saved_cards = [
            detail
            for card in saved_cards
            if (detail := self.repo.get_card(str(card.get("card_id") or "")))
            and detail.get("status") == "active"
        ]
        outbox_enqueued = self._enqueue_graphiti_jobs(
            active_saved_cards,
            signal_dates=affected_dates,
        )
        status = "ok" if saved_raw else ("partial" if errors and payload_results else ("empty" if not errors else "failed"))
        return {
            "status": status,
            "source": "portfolio_anysearch",
            "holding_count": len(holdings),
            "searched_holdings": searched_holdings,
            "fetched_items": fetched_items,
            "accepted_items": len(payload_results),
            "filtered_items": filtered_items,
            "new_raw_episodes": len(saved_raw),
            "events_upserted": len(saved_events),
            "cards_upserted": len(saved_cards),
            "affected_dates": affected_dates,
            "edge_sync": edge_sync,
            "cluster_sync": cluster_sync,
            "outbox_enqueued": outbox_enqueued,
            "errors": errors,
            "cards": saved_cards,
        }

    def _enqueue_graphiti_jobs(
        self,
        cards: Iterable[Dict[str, Any]],
        *,
        signal_dates: Iterable[str],
        market: str = "cn",
    ) -> int:
        enqueued = 0
        for card in cards:
            card_id = str(card.get("card_id") or "").strip()
            if not card_id:
                continue
            is_active = str(card.get("status") or "active") == "active"
            self.outbox_repo.enqueue(
                event_type="news_signal_card_episode" if is_active else "news_signal_card_delete",
                aggregate_id=card_id,
                payload={"card_id": card_id},
                market=market,
            )
            enqueued += 1
        for signal_date in sorted({str(value or "").strip() for value in signal_dates if str(value or "").strip()}):
            self.outbox_repo.enqueue(
                event_type="news_signal_edge_projection",
                aggregate_id=signal_date,
                payload={"signal_date": signal_date},
                market=market,
            )
            enqueued += 1
        return enqueued

    def list_cards(
        self,
        *,
        signal_date: str = "",
        signal_layer: str = "",
        industry: str = "",
        horizon: str = "",
        status: str = "",
        limit: int = 100,
    ) -> Dict[str, Any]:
        cards = self.repo.list_cards(
            signal_date=signal_date or None,
            signal_layer=signal_layer,
            industry=industry,
            horizon=horizon,
            status=status,
            limit=limit,
        )
        return {
            "schema_version": NEWS_SIGNAL_SCHEMA_VERSION,
            "total": len(cards),
            "items": cards,
            "summary": _summarize_cards(cards),
        }

    def get_card(self, card_id: str) -> Optional[Dict[str, Any]]:
        return self.repo.get_card(card_id)

    def seed_evidence_for_codes(
        self,
        codes: Iterable[str],
        *,
        signal_date: str = "",
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Return actionable news evidence only for stocks already in the seed pool."""

        requested_codes = {
            _normalize_stock_code(code)
            for code in codes
            if _normalize_stock_code(code)
        }
        if not requested_codes:
            return {
                "requested_codes": 0,
                "matched_codes": 0,
                "attached_cards": 0,
                "items_by_code": {},
                "skipped": {},
            }

        cards = self.repo.list_cards(
            signal_date=signal_date or None,
            status="active",
            limit=max(1, min(int(limit or 200), 500)),
        )
        items_by_code: Dict[str, List[Dict[str, Any]]] = {}
        skipped: Dict[str, int] = {}

        def _skip(reason: str) -> None:
            skipped[reason] = skipped.get(reason, 0) + 1

        for card in cards:
            if str(card.get("evidence_grade") or "").strip().lower() == "speculative":
                _skip("speculative")
                continue
            if str(card.get("mapping_status") or "").strip().lower() != "mapped":
                _skip("mapping_not_explicit")
                continue
            mapping_confidence = _float_or_none(card.get("mapping_confidence")) or 0.0
            if mapping_confidence < 0.65:
                _skip("low_mapping_confidence")
                continue
            signal_score = _float_or_none(card.get("adjusted_signal_score"))
            if signal_score is None:
                signal_score = _float_or_none(card.get("signal_score")) or 0.0
            if signal_score < 50.0:
                _skip("low_signal_score")
                continue
            valid_until = _parse_datetime(card.get("valid_until"))
            if valid_until is not None:
                now = datetime.now(valid_until.tzinfo) if valid_until.tzinfo else datetime.now()
                if valid_until < now:
                    _skip("expired")
                    continue

            for company in card.get("company_impacts") or []:
                if not isinstance(company, dict):
                    continue
                code = _normalize_stock_code(company.get("symbol") or company.get("code"))
                if code not in requested_codes:
                    continue
                direction = str(company.get("direction") or "").strip().lower()
                if direction != "benefit":
                    _skip("company_not_benefit")
                    continue
                company_confidence = _float_or_none(company.get("confidence")) or 0.0
                if company_confidence < 0.65:
                    _skip("low_company_confidence")
                    continue
                items_by_code.setdefault(code, []).append(
                    {
                        "card_id": card.get("card_id"),
                        "summary_short": card.get("summary_short"),
                        "signal_date": card.get("signal_date"),
                        "signal_layer": card.get("signal_layer"),
                        "impact_horizon": card.get("impact_horizon"),
                        "evidence_grade": card.get("evidence_grade"),
                        "inference_level": card.get("inference_level"),
                        "mapping_confidence": mapping_confidence,
                        "signal_score": signal_score,
                        "company_direction": direction,
                        "company_confidence": company_confidence,
                        "company_rationale": company.get("rationale"),
                        "primary_industries": card.get("primary_industries") or [],
                        "raw_episode_ids": card.get("raw_episode_ids") or [],
                        "gate_result": "matched_existing_seed",
                    }
                )

        for evidence_items in items_by_code.values():
            evidence_items.sort(
                key=lambda item: (
                    float(item.get("signal_score") or 0.0),
                    float(item.get("company_confidence") or 0.0),
                ),
                reverse=True,
            )
            del evidence_items[3:]

        return {
            "requested_codes": len(requested_codes),
            "matched_codes": len(items_by_code),
            "attached_cards": sum(len(items) for items in items_by_code.values()),
            "items_by_code": items_by_code,
            "skipped": skipped,
        }

    def add_feedback(self, *, card_id: str, feedback_type: str, note: str = "", payload: Optional[Dict[str, Any]] = None, user_id: str = "") -> Dict[str, Any]:
        feedback = self.repo.add_feedback(
            card_id=card_id,
            feedback_type=feedback_type,
            note=note,
            payload=payload or {},
            user_id=user_id,
        )
        card = self.repo.get_card(card_id)
        if card:
            feedback["card_status"] = card.get("status")
            feedback["card_effective_signal_score"] = card.get("adjusted_signal_score", card.get("signal_score"))
            try:
                if str(card.get("status") or "") == "active":
                    self.outbox_repo.enqueue(
                        event_type="news_signal_card_episode",
                        aggregate_id=card_id,
                        payload={"card_id": card_id},
                        market="cn",
                    )
                else:
                    self.outbox_repo.enqueue(
                        event_type="news_signal_card_delete",
                        aggregate_id=card_id,
                        payload={"card_id": card_id},
                        market="cn",
                    )
                signal_date = str(card.get("signal_date") or "")
                if signal_date:
                    self.outbox_repo.enqueue(
                        event_type="news_signal_edge_projection",
                        aggregate_id=signal_date,
                        payload={"signal_date": signal_date},
                        market="cn",
                    )
                feedback["graph_outbox_enqueued"] = True
            except Exception as exc:
                feedback["graph_outbox_enqueued"] = False
                feedback["graph_outbox_error"] = _compact_text(str(exc), 240)
        return feedback

    def metrics(self, *, signal_date: str = "") -> Dict[str, Any]:
        return self.repo.metrics(signal_date=signal_date or None)

    def list_edges(
        self,
        *,
        card_id: str = "",
        signal_date: str = "",
        edge_class: str = "",
        limit: int = 200,
    ) -> Dict[str, Any]:
        edges = self.repo.list_edges(card_id=card_id, signal_date=signal_date or None, edge_class=edge_class, limit=limit)
        return {
            "schema_version": NEWS_SIGNAL_SCHEMA_VERSION,
            "total": len(edges),
            "items": edges,
            "summary": {
                "edge_class_counts": _count_dict(item.get("edge_class") for item in edges),
                "edge_quality_counts": _count_dict(item.get("quality_grade") for item in edges),
                "avg_edge_quality": _avg_float(item.get("edge_quality") for item in edges),
            },
        }

    def rebuild_edges(
        self,
        *,
        signal_date: str = "",
        limit: int = 160,
        include_semantic: bool = False,
        semantic_vectors: Optional[Dict[str, List[float]]] = None,
    ) -> Dict[str, Any]:
        cards = self.repo.list_cards(signal_date=signal_date or None, status="active", limit=limit)
        scope_cards = self.repo.list_cards(
            signal_date=signal_date or None,
            limit=max(500, int(limit or 160)),
        )
        edges: List[Dict[str, Any]] = []
        edges.extend(_build_typed_relation_edges(cards))
        edges.extend(_build_event_clue_edges(cards))
        semantic_status: Dict[str, Any] = {"status": "skipped", "reason": "not_requested"}
        if include_semantic:
            semantic_edges, semantic_status = self._build_semantic_similarity_edges(cards, semantic_vectors=semantic_vectors)
            edges.extend(semantic_edges)
        result = self.repo.replace_edges_for_cards(
            [str(card.get("card_id") or "") for card in scope_cards],
            edges,
        )
        edge_counts = _count_dict(item.get("edge_class") for item in edges)
        edge_quality_counts = _count_dict(item.get("quality_grade") for item in edges)
        return {
            "status": "ok",
            "cards": len(cards),
            "edges_upserted": result.get("edges_upserted", 0),
            "edge_class_counts": edge_counts,
            "edge_quality_counts": edge_quality_counts,
            "semantic": semantic_status,
        }

    def card_graph(self, card_id: str, *, limit: int = 200) -> Dict[str, Any]:
        center = self.repo.get_card(card_id)
        if not center:
            raise ValueError(f"news signal card not found: {card_id}")
        edges = self.repo.list_edges(card_id=card_id, limit=limit)
        card_ids = {str(center.get("card_id") or "")}
        target_nodes: Dict[str, Dict[str, Any]] = {}
        for edge in edges:
            source_card_id = str(edge.get("source_card_id") or "")
            target_card_id = str(edge.get("target_card_id") or "")
            if source_card_id:
                card_ids.add(source_card_id)
            if target_card_id:
                card_ids.add(target_card_id)
            if edge.get("target_type") != "card":
                node_id = str(edge.get("target_id") or "")
                if node_id:
                    target_nodes[node_id] = {
                        "id": node_id,
                        "type": edge.get("target_type") or "entity",
                        "label": _target_label(node_id),
                    }

        nodes: List[Dict[str, Any]] = []
        card_details: Dict[str, Dict[str, Any]] = {}
        for related_card_id in sorted(card_ids):
            card = center if related_card_id == center.get("card_id") else self.repo.get_card(related_card_id)
            if not card:
                continue
            card_details[related_card_id] = card
            raw_episodes = card.get("raw_episodes") if isinstance(card.get("raw_episodes"), list) else []
            nodes.append(
                {
                    "id": str(card.get("card_id") or ""),
                    "type": "card",
                    "label": str(card.get("summary_short") or card.get("card_id") or "")[:80],
                    "signal_date": card.get("signal_date"),
                    "signal_layer": card.get("signal_layer"),
                    "signal_score": card.get("signal_score"),
                    "transmission_paths": (card.get("transmission_paths") or [])[:3],
                    "source_previews": [
                        {
                            "title": raw.get("title"),
                            "published_at": raw.get("published_at"),
                            "source": raw.get("source"),
                            "url": raw.get("url"),
                        }
                        for raw in raw_episodes[:3]
                        if isinstance(raw, dict)
                    ],
                }
            )
        nodes.extend(target_nodes.values())
        enriched_edges: List[Dict[str, Any]] = []
        center_id = str(center.get("card_id") or "")
        for edge in edges:
            enriched = dict(edge)
            target_card_id = str(edge.get("target_card_id") or "").strip()
            target_card = card_details.get(target_card_id)
            if target_card:
                enriched["target_label"] = str(target_card.get("summary_short") or target_card_id)[:140]
                enriched["target_signal_date"] = target_card.get("signal_date")
                enriched["target_transmission_paths"] = (target_card.get("transmission_paths") or [])[:3]
            elif edge.get("target_type") != "card":
                enriched["target_label"] = _target_label(str(edge.get("target_id") or ""))

            source_card_id = str(edge.get("source_card_id") or "").strip()
            related_card_id = target_card_id if source_card_id == center_id else source_card_id
            related_card = card_details.get(related_card_id)
            if related_card and related_card_id != center_id:
                enriched["related_card_id"] = related_card_id
                enriched["related_label"] = str(related_card.get("summary_short") or related_card_id)[:140]
                enriched["related_signal_date"] = related_card.get("signal_date")
                enriched["related_transmission_paths"] = (related_card.get("transmission_paths") or [])[:3]
            enriched_edges.append(enriched)
        return {
            "schema_version": NEWS_SIGNAL_SCHEMA_VERSION,
            "center_card_id": str(center.get("card_id") or ""),
            "nodes": nodes,
            "edges": enriched_edges,
            "summary": {
                "node_count": len(nodes),
                "edge_count": len(edges),
                "edge_class_counts": _count_dict(item.get("edge_class") for item in edges),
                "edge_quality_counts": _count_dict(item.get("quality_grade") for item in edges),
                "avg_edge_quality": _avg_float(item.get("edge_quality") for item in edges),
            },
        }

    def refresh_outcomes(self) -> Dict[str, Any]:
        return self.repo.refresh_outcomes_from_seed_evaluations()

    def backfill_extracted_events(
        self,
        *,
        signal_date: str = "",
        limit: int = 500,
        only_missing: bool = True,
    ) -> Dict[str, Any]:
        """Backfill structured events from the relational source of truth."""

        cards = self.repo.list_cards(
            signal_date=signal_date or None,
            limit=max(1, min(int(limit or 500), 500)),
        )
        cards_updated = 0
        events_upserted = 0
        skipped_existing = 0
        errors: List[Dict[str, Any]] = []

        for summary in cards:
            card_id = str(summary.get("card_id") or "").strip()
            if not card_id:
                continue
            try:
                card = self.repo.get_card(card_id)
                if not card:
                    continue
                existing_events = card.get("extracted_events") if isinstance(card.get("extracted_events"), list) else []
                if only_missing and existing_events:
                    skipped_existing += 1
                    continue
                raw_episodes = card.get("raw_episodes") if isinstance(card.get("raw_episodes"), list) else []
                raw = raw_episodes[0] if raw_episodes else {
                    "episode_id": (card.get("raw_episode_ids") or [f"card:{card_id}"])[0],
                    "source": "news_signal_card",
                    "signal_date": card.get("signal_date"),
                    "title": card.get("summary_short"),
                    "summary": card.get("summary_short"),
                }
                text = _normalize_news_content(
                    card.get("summary_short"),
                    raw.get("normalized_content"),
                    raw.get("summary"),
                    raw.get("content"),
                    raw.get("title"),
                )
                primary_industries = _unique_strings(card.get("primary_industries") or [])
                secondary_industries = _unique_strings(card.get("secondary_industries") or [])
                company_impacts = card.get("company_impacts") if isinstance(card.get("company_impacts"), list) else []
                events = _extract_news_events(
                    raw=raw,
                    card_id=card_id,
                    primary_theme=primary_industries[0] if primary_industries else "实时消息",
                    text=text or str(card.get("summary_short") or ""),
                    related_boards=secondary_industries,
                    company_impacts=company_impacts,
                    tone=str(card.get("news_tone") or "neutral"),
                    mapping_confidence=_float_or_none(card.get("mapping_confidence")) or 0.0,
                    subject_names=_unique_strings(raw.get("subjects") or []),
                    llm_extractor=self._event_llm_extractor,
                )
                if not events:
                    errors.append({"card_id": card_id, "error": "no_events_extracted"})
                    continue
                saved_events = self.repo.upsert_extracted_events(events)
                transmission_paths = _transmission_paths(
                    primary_industries[0] if primary_industries else "实时消息",
                    secondary_industries,
                    company_impacts,
                    text or str(card.get("summary_short") or ""),
                    extracted_events=events,
                )
                diagnostics = card.get("diagnostics") if isinstance(card.get("diagnostics"), dict) else {}
                diagnostics["event_extraction"] = _event_extraction_summary(events)
                diagnostics["event_backfill"] = {
                    "backfilled_at": datetime.now().isoformat(),
                    "raw_episode_id": raw.get("episode_id"),
                }
                self.repo.update_event_projection(
                    card_id,
                    transmission_paths=transmission_paths,
                    diagnostics=diagnostics,
                )
                cards_updated += 1
                events_upserted += len(saved_events)
            except Exception as exc:
                logger.warning("news event backfill failed for %s: %s", card_id, exc, exc_info=True)
                errors.append({"card_id": card_id, "error": str(exc)})

        return {
            "status": "ok" if not errors else ("partial" if cards_updated else "failed"),
            "signal_date": signal_date or None,
            "cards_scanned": len(cards),
            "cards_updated": cards_updated,
            "events_upserted": events_upserted,
            "skipped_existing": skipped_existing,
            "errors": errors,
        }

    def repair_company_mapping_gates(
        self,
        *,
        signal_date: str = "",
        limit: int = 500,
    ) -> Dict[str, Any]:
        """Remove legacy company mappings that are not explicit in source text."""

        cards = self.repo.list_cards(
            signal_date=signal_date or None,
            limit=max(1, min(int(limit or 500), 500)),
        )
        updated_cards: List[Dict[str, Any]] = []
        companies_removed = 0
        for summary in cards:
            card_id = str(summary.get("card_id") or "").strip()
            card = self.repo.get_card(card_id) if card_id else None
            if not card:
                continue
            existing_impacts = card.get("company_impacts") if isinstance(card.get("company_impacts"), list) else []
            raw_episodes = card.get("raw_episodes") if isinstance(card.get("raw_episodes"), list) else []
            source_text = _normalize_news_content(
                card.get("summary_short"),
                *[
                    value
                    for raw in raw_episodes
                    if isinstance(raw, dict)
                    for value in (
                        raw.get("normalized_content"),
                        raw.get("title"),
                        raw.get("summary"),
                        raw.get("content"),
                    )
                ],
            )
            accepted_impacts = _stocks_explicitly_mentioned(existing_impacts, source_text)
            generic_company_teaser = _is_unresolved_company_teaser(source_text, accepted_impacts)
            if len(accepted_impacts) == len(existing_impacts) and not generic_company_teaser:
                continue
            removed_count = len(existing_impacts) - len(accepted_impacts)
            companies_removed += removed_count
            mapping_confidence = max(
                (_float_or_none(item.get("confidence")) or 0.0 for item in accepted_impacts),
                default=0.35,
            )
            mapping_status = "mapped" if accepted_impacts else "industry_only"
            signal_layer = "macro" if _macro_theme_for_text(source_text) else ("company" if accepted_impacts else "industry")
            current_score = _float_or_none(card.get("signal_score")) or 0.0
            signal_score = current_score if accepted_impacts else min(current_score, 49.0)
            if generic_company_teaser:
                signal_score = min(signal_score, 35.0)
            evidence_grade = str(card.get("evidence_grade") or "plausible")
            if not accepted_impacts and evidence_grade == "confirmed":
                evidence_grade = "plausible" if card.get("primary_industries") else "speculative"
            if generic_company_teaser:
                evidence_grade = "speculative"
            diagnostics = dict(card.get("diagnostics") or {})
            diagnostics["company_mapping_gate"] = {
                **_company_mapping_gate(
                    candidate_count=len(existing_impacts),
                    accepted_count=len(accepted_impacts),
                ),
                "repaired_at": datetime.now().isoformat(),
                "removed_count": removed_count,
                "suppression_reason": "generic_company_teaser_without_explicit_company" if generic_company_teaser else "",
            }
            extracted_events = card.get("extracted_events") if isinstance(card.get("extracted_events"), list) else []
            primary_industries = _unique_strings(card.get("primary_industries") or [])
            transmission_paths = _transmission_paths(
                primary_industries[0] if primary_industries else "实时消息",
                _unique_strings(card.get("secondary_industries") or []),
                accepted_impacts,
                source_text,
                extracted_events=extracted_events,
            )
            saved = self.repo.update_company_mapping_projection(
                card_id,
                company_impacts=accepted_impacts,
                mapping_status=mapping_status,
                mapping_confidence=mapping_confidence,
                signal_layer=signal_layer,
                signal_score=signal_score,
                evidence_grade=evidence_grade,
                status="suppressed" if generic_company_teaser else str(card.get("status") or "active"),
                transmission_paths=transmission_paths,
                diagnostics=diagnostics,
            )
            if saved:
                updated_cards.append(saved)

        affected_dates = sorted(
            {
                str(card.get("signal_date") or "")
                for card in updated_cards
                if card.get("signal_date")
            }
        )
        outbox_enqueued = self._enqueue_graphiti_jobs(
            updated_cards,
            signal_dates=affected_dates,
        )
        return {
            "status": "ok",
            "cards_scanned": len(cards),
            "cards_updated": len(updated_cards),
            "companies_removed": companies_removed,
            "affected_dates": affected_dates,
            "outbox_enqueued": outbox_enqueued,
        }

    def reconcile_same_event_clusters(
        self,
        *,
        signal_date: str = "",
        limit: int = 500,
    ) -> Dict[str, Any]:
        """Merge strong same-event card clusters into one canonical active card."""

        capped_limit = max(1, min(int(limit or 500), 500))
        self.rebuild_edges(signal_date=signal_date, limit=capped_limit)
        cards = self.repo.list_cards(
            signal_date=signal_date or None,
            status="active",
            limit=capped_limit,
        )
        card_by_id = {
            str(card.get("card_id") or ""): card
            for card in cards
            if str(card.get("card_id") or "").strip()
        }
        edges = self.repo.list_edges(
            signal_date=signal_date or None,
            edge_class="event_clue",
            limit=10000,
        )
        adjacency: Dict[str, set[str]] = {}
        for edge in edges:
            if str(edge.get("edge_type") or "") != "same_event":
                continue
            source_id = str(edge.get("source_card_id") or "").strip()
            target_id = str(edge.get("target_card_id") or "").strip()
            if source_id not in card_by_id or target_id not in card_by_id:
                continue
            adjacency.setdefault(source_id, set()).add(target_id)
            adjacency.setdefault(target_id, set()).add(source_id)

        visited: set[str] = set()
        clusters: List[List[str]] = []
        for card_id in sorted(adjacency):
            if card_id in visited:
                continue
            stack = [card_id]
            members: List[str] = []
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                members.append(current)
                stack.extend(sorted(adjacency.get(current, set()) - visited))
            if len(members) > 1:
                clusters.append(sorted(members))

        canonical_cards: List[Dict[str, Any]] = []
        suppressed = 0
        cluster_details = []
        for members in clusters:
            canonical_id = max(
                members,
                key=lambda value: (
                    _card_signal_score(card_by_id[value]),
                    _card_quality_score(card_by_id[value]),
                    str(card_by_id[value].get("signal_date") or ""),
                ),
            )
            duplicate_ids = [value for value in members if value != canonical_id]
            cluster_id = _stable_id("event-cluster", *members)
            canonical = self.repo.merge_same_event_cluster(
                canonical_card_id=canonical_id,
                duplicate_card_ids=duplicate_ids,
                cluster_id=cluster_id,
            )
            if not canonical:
                continue
            canonical_cards.append(canonical)
            suppressed += len(duplicate_ids)
            cluster_details.append(
                {
                    "cluster_id": cluster_id,
                    "canonical_card_id": canonical_id,
                    "duplicate_card_ids": duplicate_ids,
                }
            )

        affected_dates = sorted(
            {
                str(card.get("signal_date") or "")
                for card in canonical_cards
                if card.get("signal_date")
            }
        )
        edge_sync = [
            self.rebuild_edges(signal_date=value, limit=capped_limit)
            for value in affected_dates
        ]
        outbox_enqueued = self._enqueue_graphiti_jobs(
            canonical_cards,
            signal_dates=affected_dates,
        )
        return {
            "status": "ok",
            "cards_scanned": len(cards),
            "clusters_merged": len(canonical_cards),
            "cards_suppressed": suppressed,
            "clusters": cluster_details,
            "edge_sync": edge_sync,
            "outbox_enqueued": outbox_enqueued,
        }

    def sync_graphiti(
        self,
        *,
        card_ids: Optional[List[str]] = None,
        signal_date: str = "",
        limit: int = 100,
        include_semantic_edges: bool = False,
        include_episodes: bool = True,
    ) -> Dict[str, Any]:
        """Synchronize news signal cards into Graphiti episodes."""

        try:
            from src.services.graphiti.graph_service import get_graphiti_service
        except Exception as exc:
            return {"status": "disabled", "reason": f"graphiti import failed: {exc}", "synced": 0, "failed": 0, "skipped": 0}

        graphiti = get_graphiti_service()
        if not graphiti.is_available():
            return {"status": "disabled", "reason": "graphiti_unavailable", "synced": 0, "failed": 0, "skipped": 0}

        cards: List[Dict[str, Any]] = []
        if card_ids:
            seen: set[str] = set()
            for card_id in card_ids:
                key = str(card_id or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                card = self.repo.get_card(key)
                if card:
                    cards.append(card)
        else:
            cards = (
                self.repo.list_graph_sync_candidates(signal_date=signal_date or None, limit=limit)
                if include_episodes
                else self.repo.list_cards(signal_date=signal_date or None, limit=limit)
            )

        edge_sync = {"status": "skipped", "reason": "no_cards"}
        if cards:
            edge_sync = self.rebuild_edges(
                signal_date=signal_date,
                limit=max(limit, len(cards)),
                include_semantic=include_semantic_edges,
            )
        graph_edges = _collect_graph_edges(
            self.repo,
            card_ids=[str(card.get("card_id") or "") for card in cards],
            signal_date=signal_date,
            limit=max(2000, min(10000, len(cards) * 40)),
        )

        synced = 0
        failed = 0
        skipped = 0
        details: List[Dict[str, Any]] = []
        episode_sync: Dict[str, Any] = {"status": "skipped", "reason": "disabled_by_request"}
        if include_episodes:
            for card in cards[: max(1, min(int(limit or 100), 500))]:
                card_id = str(card.get("card_id") or "")
                if card.get("status") != "active":
                    skipped += 1
                    details.append({"card_id": card_id, "status": "skipped", "reason": "card_not_active"})
                    continue
                result = graphiti.ingest_news_signal_card_sync(card=card, market="cn")
                result_status = str(result.get("status") or "")
                if result_status == "synced":
                    self.repo.update_graph_sync_status(card_id, status="synced")
                    synced += 1
                elif result_status == "failed":
                    self.repo.update_graph_sync_status(card_id, status="failed", error=str(result.get("error") or "graphiti sync failed"))
                    failed += 1
                else:
                    skipped += 1
                details.append({"card_id": card_id, **result})
            episode_sync = {
                "status": "ok" if failed == 0 else ("partial" if synced or skipped else "failed"),
                "requested": len(cards),
                "synced": synced,
                "failed": failed,
                "skipped": skipped,
            }

        graph_edge_sync = {"status": "skipped", "reason": "no_edges"}
        if graph_edges and hasattr(graphiti, "sync_news_signal_edges_sync"):
            try:
                graph_edge_sync = graphiti.sync_news_signal_edges_sync(cards=cards, edges=graph_edges, market="cn")
            except Exception as exc:
                graph_edge_sync = {"status": "failed", "error": str(exc)}

        phase_statuses = {
            str(episode_sync.get("status") or "skipped"),
            str(edge_sync.get("status") or "skipped"),
            str(graph_edge_sync.get("status") or "skipped"),
        }
        incomplete_statuses = phase_statuses - {"ok", "skipped"}
        if "failed" in phase_statuses:
            status = "partial" if {"ok", "skipped"} & phase_statuses else "failed"
        elif incomplete_statuses or failed:
            status = "partial"
        else:
            status = "ok"

        return {
            "status": status,
            "requested": len(cards),
            "synced": synced,
            "failed": failed,
            "skipped": skipped,
            "episode_sync": episode_sync,
            "edge_sync": edge_sync,
            "graph_edge_sync": graph_edge_sync,
            "details": details,
        }

    def _build_semantic_similarity_edges(
        self,
        cards: List[Dict[str, Any]],
        *,
        semantic_vectors: Optional[Dict[str, List[float]]] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        if len(cards) < 2:
            return [], {"status": "skipped", "reason": "not_enough_cards"}

        config = get_config()
        model = str(getattr(config, "graphiti_embedding_model", "") or "").strip()
        vectors = semantic_vectors or {}
        if not vectors:
            if not model:
                return [], {"status": "disabled", "reason": "embedding_model_unconfigured"}
            try:
                from src.services.graphiti.litellm_embedder import LiteLLMGraphitiEmbedder

                texts = [_news_edge_text(card) for card in cards]
                embedder = LiteLLMGraphitiEmbedder(embedding_model=model)
                vectors_list = _run_async(embedder.create_batch(texts))
                vectors = {
                    str(card.get("card_id") or ""): [float(value) for value in vector]
                    for card, vector in zip(cards, vectors_list)
                }
            except Exception as exc:
                return [], {"status": "failed", "reason": str(exc), "embedding_model": model}

        by_id = {str(card.get("card_id") or ""): card for card in cards if card.get("card_id")}
        edges: List[Dict[str, Any]] = []
        threshold_profile = resolve_semantic_threshold(
            model,
            profiles_json=str(getattr(config, "news_signal_embedding_thresholds_json", "") or ""),
        )
        threshold = float(threshold_profile["threshold"])
        similarities: List[float] = []
        for idx, left in enumerate(cards):
            left_id = str(left.get("card_id") or "")
            left_vec = vectors.get(left_id)
            if not left_id or not left_vec:
                continue
            for right in cards[idx + 1:]:
                right_id = str(right.get("card_id") or "")
                right_vec = vectors.get(right_id)
                if not right_id or not right_vec:
                    continue
                similarity = _cosine_similarity(left_vec, right_vec)
                similarities.append(similarity)
                if similarity < threshold:
                    continue
                source, target = _ordered_card_pair(by_id[left_id], by_id[right_id])
                source_card = by_id[source]
                target_card = by_id[target]
                evidence = _edge_pair_evidence(source_card, target_card)
                evidence.update({"similarity": round(similarity, 6), "threshold": threshold})
                edge = _edge_payload(
                    source_card_id=source,
                    target_type="card",
                    target_id=target,
                    target_card_id=target,
                    edge_class="semantic_similarity",
                    edge_type="semantic_similarity",
                    weight=similarity,
                    method="embedding",
                    rationale=f"新闻卡片语义相似度 {similarity:.3f}，仅作为弱语义线索，需要实体或事件证据确认。",
                    evidence=evidence,
                    embedding_model=model or "test-vector",
                    threshold_profile=str(threshold_profile["profile"]),
                    decay_rule="14d",
                )
                if float(edge.get("edge_quality") or 0.0) >= SEMANTIC_EDGE_MIN_QUALITY:
                    edges.append(edge)
        edges = _limit_semantic_edges(edges, per_card_limit=SEMANTIC_EDGE_TOP_K_PER_CARD)
        return edges, {
            "status": "ok",
            "embedding_model": model or "provided_vectors",
            "threshold": threshold,
            "threshold_profile": threshold_profile,
            "similarity_distribution": _similarity_distribution(similarities),
            "min_quality": SEMANTIC_EDGE_MIN_QUALITY,
            "top_k_per_card": SEMANTIC_EDGE_TOP_K_PER_CARD,
            "edges": len(edges),
        }

    def evidence_card_for(self, card_id: str, *, symbol: str = "", name: str = "", run_id: str = "news-signal") -> Dict[str, Any]:
        card = self.repo.get_card(card_id)
        if not card:
            raise ValueError(f"news signal card not found: {card_id}")
        evidence = news_signal_to_evidence_card(card, symbol=symbol, name=name, run_id=run_id)
        return evidence.model_dump(mode="json")

    def _fetch_cjzc(self, target: date) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        try:
            from src.agent.tools.data_tools import get_eastmoney_cjzc_daily_tool

            result = get_eastmoney_cjzc_daily_tool.handler(target_date=target.isoformat(), allow_previous=True)
            if not isinstance(result, dict) or result.get("status") in {"failed", "missing"}:
                return result if isinstance(result, dict) else None, {
                    "source": "news_theme_daily",
                    "error": str((result or {}).get("errors") or "empty Eastmoney CJZC response"),
                }
            return result, None
        except Exception as exc:
            return None, {"source": "news_theme_daily", "error": str(exc)}

    def _fetch_cls(self, *, limit: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        try:
            from src.agent.tools.search_tools import get_cls_telegraph_news_tool

            result = get_cls_telegraph_news_tool.handler(limit=max(1, min(int(limit or 50), 50)))
            if not isinstance(result, dict) or result.get("status") == "error":
                return result if isinstance(result, dict) else None, {
                    "source": "cls_telegraph",
                    "error": str((result or {}).get("errors") or "empty CLS telegraph response"),
                }
            return result, None
        except Exception as exc:
            return None, {"source": "cls_telegraph", "error": str(exc)}

    def _fetch_xueqiu(self, *, limit: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        try:
            from src.agent.tools.search_tools import get_xueqiu_hot_news_tool

            result = get_xueqiu_hot_news_tool.handler(limit=max(1, min(int(limit or 30), 50)))
            if not isinstance(result, dict) or result.get("status") == "error":
                return result if isinstance(result, dict) else None, {
                    "source": "xueqiu_hot",
                    "error": str((result or {}).get("errors") or "empty Xueqiu hot news response"),
                }
            return result, None
        except Exception as exc:
            return None, {"source": "xueqiu_hot", "error": str(exc)}

    def _fetch_macro_finance(self, *, limit: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        try:
            from src.agent.tools.search_tools import get_macro_finance_news_tool

            result = get_macro_finance_news_tool.handler(limit=max(1, min(int(limit or 30), 50)))
            if not isinstance(result, dict) or result.get("status") == "error":
                return result if isinstance(result, dict) else None, {
                    "source": "macro_finance",
                    "error": str((result or {}).get("errors") or "empty macro finance response"),
                }
            return result, None
        except Exception as exc:
            return None, {"source": "macro_finance", "error": str(exc)}

    def _build_from_cjzc(self, payload: Dict[str, Any], target: date) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        now = datetime.now()
        publish_dt = _parse_datetime(payload.get("publish_time")) or datetime.combine(target, time(hour=6))
        raw_rows: List[Dict[str, Any]] = []
        cards: List[Dict[str, Any]] = []
        for item in payload.get("themes") or []:
            if not isinstance(item, dict):
                continue
            raw = _cjzc_theme_raw_episode(payload, item, target=target, published_at=publish_dt, ingested_at=now)
            _attach_raw_quality(raw, source=str(raw.get("source") or "news_theme_daily"))
            raw_rows.append(raw)
            cards.append(self._card_from_theme_item(item, raw, target=target, source="news_theme_daily"))
        return raw_rows, cards

    def _build_from_cls(self, payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        return self._build_from_dailynews(payload, source="cls_telegraph", raw_prefix="cls", default_title="财联社电报")

    def _build_from_xueqiu(self, payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        return self._build_from_dailynews(payload, source="xueqiu_hot", raw_prefix="xueqiu", default_title="雪球热榜")

    def _build_from_macro_finance(self, payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        return self._build_from_dailynews(payload, source="macro_finance", raw_prefix="macro", default_title="宏观财经")

    def _build_from_dailynews(
        self,
        payload: Dict[str, Any],
        *,
        source: str,
        raw_prefix: str,
        default_title: str,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        raw_rows: List[Dict[str, Any]] = []
        cards: List[Dict[str, Any]] = []
        now = datetime.now()
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            published_at = _parse_datetime(item.get("published_at")) or now
            signal_date, session = _signal_date_and_session(published_at)
            text = " ".join(
                str(part or "")
                for part in (
                    item.get("title"),
                    item.get("brief"),
                    item.get("content"),
                    " ".join(item.get("subject_names") or []),
                    " ".join(str(stock.get("name") or "") for stock in item.get("stocks") or [] if isinstance(stock, dict)),
                )
            )
            episode_id = _stable_id(f"raw:{raw_prefix}", item.get("id"), item.get("url"), item.get("title"))
            raw = {
                "episode_id": episode_id,
                "dedup_key": _stable_id(f"dedup:{raw_prefix}", item.get("id"), item.get("url"), item.get("title"), item.get("published_at")),
                "source": source,
                "provider": str(item.get("provider") or payload.get("provider") or source),
                "source_id": str(item.get("id") or ""),
                "url": str(item.get("url") or ""),
                "title": str(item.get("title") or default_title),
                "summary": str(item.get("brief") or item.get("snippet") or ""),
                "content": str(item.get("content") or ""),
                "published_at": published_at,
                "ingested_at": now,
                "signal_date": signal_date,
                "session": session,
                "subjects": item.get("subjects") or [],
                "stocks": item.get("stocks") or [],
                "source_chain": item.get("source_chain")
                or payload.get("source_chain")
                or [{"provider": payload.get("provider") or source, "result": payload.get("status") or "ok"}],
                "raw_payload": item,
                "status": "ok",
                "errors": [],
            }
            _attach_raw_quality(raw, source=source, candidate_text=text)
            raw_rows.append(raw)
            card_text = " ".join(
                part
                for part in [
                    str(raw.get("normalized_content") or ""),
                    " ".join(item.get("subject_names") or []),
                    " ".join(str(stock.get("name") or "") for stock in item.get("stocks") or [] if isinstance(stock, dict)),
                ]
                if part
            )
            cards.append(self._card_from_dailynews_item(item, raw, text=card_text, source=source, raw_prefix=raw_prefix))
        return raw_rows, cards

    def _card_from_theme_item(self, item: Dict[str, Any], raw: Dict[str, Any], *, target: date, source: str) -> Dict[str, Any]:
        theme = str(item.get("theme") or "未分类主题").strip()
        evidence = str(item.get("evidence") or raw.get("summary") or raw.get("title") or "")
        concept_payload = self._concept_mapping.get(theme, {}) if isinstance(self._concept_mapping.get(theme), dict) else {}
        related_boards = _unique_strings(item.get("related_boards") or concept_payload.get("related_boards") or [])
        mapped_stocks = item.get("mapped_stocks") if isinstance(item.get("mapped_stocks"), list) else concept_payload.get("mapped_stocks") or []
        mapped_stock_count = len(mapped_stocks)
        mapped_stocks = _stocks_explicitly_mentioned(mapped_stocks, evidence)
        tone = _tone_from_polarity(item.get("polarity") or _infer_tone(evidence))
        horizon, decay, valid_until = _horizon_for_text(evidence, datetime.combine(target, time(hour=9)))
        company_impacts, mapping_status, mapping_confidence = _company_impacts_from_mapped_stocks(mapped_stocks, tone)
        score = _score_from_parts(
            source=source,
            tone=tone,
            theme_score=item.get("theme_score"),
            company_count=len(company_impacts),
            evidence_grade="plausible",
            mapping_confidence=mapping_confidence,
        )
        raw_quality = _raw_quality_summary(raw)
        adjusted_score = _score_with_quality(score, raw_quality)
        card_status = _status_from_quality(raw_quality)
        evidence_grade = "speculative" if card_status == "low_quality" else "plausible"
        card_id = _stable_id("card:cjzc", target.isoformat(), theme)
        extracted_events = _extract_news_events(
            raw=raw,
            card_id=card_id,
            primary_theme=theme,
            text=evidence,
            related_boards=related_boards,
            company_impacts=company_impacts,
            tone=tone,
            mapping_confidence=mapping_confidence,
            llm_extractor=self._event_llm_extractor,
        )
        return {
            "card_id": card_id,
            "signal_date": target,
            "session": "pre_open",
            "signal_layer": _classify_signal_layer(
                text=f"{theme} {evidence}",
                company_impacts=company_impacts,
                explicit_company_impacts=[],
                primary_industries=[theme],
            ),
            "summary_short": _compact_text(f"{theme}: {evidence}", 140),
            "news_tone": tone,
            "market_impact": _market_impact_from_tone(tone),
            "impact_horizon": horizon,
            "valid_from": datetime.combine(target, time(hour=9)),
            "valid_until": valid_until,
            "decay_rule": decay,
            "refresh_trigger": _refresh_trigger_for_horizon(horizon),
            "staleness_score": 0.0,
            "evidence_grade": evidence_grade,
            "inference_level": "first_order" if company_impacts else "explicit",
            "mapping_status": mapping_status,
            "mapping_confidence": mapping_confidence,
            "signal_score": adjusted_score,
            "status": card_status,
            "primary_industries": [theme],
            "secondary_industries": related_boards[:6],
            "explicit_entities": _unique_strings([theme] + list(item.get("keywords") or [])),
            "industry_impacts": [_industry_impact(theme, tone, evidence, strength="medium")],
            "company_impacts": company_impacts,
            "transmission_paths": _transmission_paths(theme, related_boards, company_impacts, evidence, extracted_events=extracted_events),
            "raw_episode_ids": [raw["episode_id"]],
            "source_chain": raw.get("source_chain") or [],
            "extracted_events": extracted_events,
            "diagnostics": {
                "schema_version": NEWS_SIGNAL_SCHEMA_VERSION,
                "source": source,
                "signal_layer": _classify_signal_layer(
                    text=f"{theme} {evidence}",
                    company_impacts=company_impacts,
                    explicit_company_impacts=[],
                    primary_industries=[theme],
                ),
                "keywords": item.get("keywords") or [],
                "high_impact_terms": item.get("high_impact_terms") or [],
                "mapping_source": "concept_mapping.json",
                "company_mapping_gate": _company_mapping_gate(
                    candidate_count=mapped_stock_count,
                    accepted_count=len(company_impacts),
                ),
                "raw_quality": raw_quality,
                "quality_gate": _quality_gate(raw_quality),
                "event_extraction": _event_extraction_summary(extracted_events),
            },
            "source_count": 1,
            "graph_sync_status": "pending",
        }

    def _card_from_dailynews_item(self, item: Dict[str, Any], raw: Dict[str, Any], *, text: str, source: str, raw_prefix: str) -> Dict[str, Any]:
        signal_date = _parse_date(raw.get("signal_date")) or datetime.now().date()
        published_at = raw.get("published_at") if isinstance(raw.get("published_at"), datetime) else datetime.now()
        concepts = self._match_concepts(text)
        subject_names = _unique_strings(item.get("subject_names") or [])
        macro_theme = _macro_theme_for_text(text)
        primary = [macro_theme] if macro_theme else ([concept for concept, _ in concepts[:2]] or subject_names[:2] or ["实时快讯"])
        secondary: List[str] = []
        mapped_stocks: List[Dict[str, Any]] = []
        for concept, payload in ([] if macro_theme else concepts):
            secondary.extend(_unique_strings(payload.get("related_boards") or []))
            mapped = payload.get("mapped_stocks") if isinstance(payload.get("mapped_stocks"), list) else []
            mapped_stocks.extend(mapped[:5])
        source_stocks = item.get("stocks") if isinstance(item.get("stocks"), list) else []
        company_candidate_count = len(source_stocks) + len(mapped_stocks)
        explicit_stocks = _stocks_explicitly_mentioned(source_stocks, text)
        explicitly_mapped_stocks = _stocks_explicitly_mentioned(mapped_stocks, text)
        tone = _tone_from_polarity(item.get("polarity"))
        if tone == "neutral":
            tone = _infer_tone(text)
        positive_quality = _positive_signal_quality(text)
        tone_override = str(positive_quality.get("tone_override") or "")
        if tone_override and tone in {"positive", "mixed", "neutral"}:
            tone = tone_override
        explicit_company_impacts, explicit_status, explicit_confidence = _company_impacts_from_cls_stocks(explicit_stocks, tone)
        mapped_company_impacts, mapped_status, mapped_confidence = _company_impacts_from_mapped_stocks(explicitly_mapped_stocks, tone)
        company_impacts = _merge_company_impacts(explicit_company_impacts + mapped_company_impacts)
        company_mapping_gate = _company_mapping_gate(
            candidate_count=company_candidate_count,
            accepted_count=len(company_impacts),
        )
        generic_company_teaser = _is_unresolved_company_teaser(text, company_impacts)
        mapping_status, mapping_confidence = _combine_mapping_status(
            explicit_status,
            explicit_confidence,
            mapped_status,
            mapped_confidence,
            has_industry=bool(primary),
        )
        horizon, decay, valid_until = _horizon_for_text(text, published_at)
        raw_quality = _raw_quality_summary(raw)
        evidence_grade = "confirmed" if explicit_company_impacts else ("plausible" if concepts or macro_theme else "speculative")
        if _status_from_quality(raw_quality) == "low_quality":
            evidence_grade = "speculative"
        evidence_grade_override = str(positive_quality.get("evidence_grade_override") or "")
        if evidence_grade_override:
            evidence_grade = evidence_grade_override
        signal_layer = _classify_signal_layer(
            text=text,
            company_impacts=company_impacts,
            explicit_company_impacts=explicit_company_impacts,
            primary_industries=primary,
        )
        base_score = _score_from_parts(
            source=source,
            tone=tone,
            theme_score=18.0 if item.get("is_important") else 8.0,
            company_count=len(company_impacts),
            evidence_grade=evidence_grade,
            mapping_confidence=mapping_confidence,
        )
        adjusted_score = _score_with_quality(base_score, raw_quality)
        adjusted_score = _apply_positive_signal_quality_score(adjusted_score, positive_quality)
        if generic_company_teaser:
            adjusted_score = min(adjusted_score, 35.0)
            evidence_grade = "speculative"
        card_id = _stable_id(f"card:{raw_prefix}", item.get("id"), item.get("url"), item.get("title"), item.get("published_at"))
        extracted_events = _extract_news_events(
            raw=raw,
            card_id=card_id,
            primary_theme=primary[0] if primary else "实时快讯",
            text=text,
            related_boards=_unique_strings(secondary),
            company_impacts=company_impacts,
            tone=tone,
            mapping_confidence=mapping_confidence,
            subject_names=subject_names,
            llm_extractor=self._event_llm_extractor,
        )
        return {
            "card_id": card_id,
            "signal_date": signal_date,
            "session": raw.get("session") or "intraday",
            "signal_layer": signal_layer,
            "summary_short": _compact_text(str(item.get("brief") or item.get("content") or item.get("title") or ""), 140),
            "news_tone": tone,
            "market_impact": _market_impact_from_tone(tone),
            "impact_horizon": horizon,
            "valid_from": published_at,
            "valid_until": valid_until,
            "decay_rule": decay,
            "refresh_trigger": _refresh_trigger_for_horizon(horizon),
            "staleness_score": 0.0,
            "evidence_grade": evidence_grade,
            "inference_level": "explicit" if explicit_company_impacts else ("first_order" if concepts else "explicit"),
            "mapping_status": mapping_status,
            "mapping_confidence": mapping_confidence,
            "signal_score": adjusted_score,
            "status": "suppressed" if generic_company_teaser else _status_from_quality(raw_quality),
            "primary_industries": primary,
            "secondary_industries": _unique_strings(secondary)[:6],
            "explicit_entities": _unique_strings(subject_names + [stock.get("name") for stock in item.get("stocks") or [] if isinstance(stock, dict)]),
            "industry_impacts": [_industry_impact(industry, tone, text, strength="medium") for industry in primary],
            "company_impacts": company_impacts,
            "transmission_paths": _transmission_paths(primary[0], _unique_strings(secondary), company_impacts, text, extracted_events=extracted_events) if primary else [],
            "raw_episode_ids": [raw["episode_id"]],
            "source_chain": raw.get("source_chain") or [],
            "extracted_events": extracted_events,
            "diagnostics": {
                "schema_version": NEWS_SIGNAL_SCHEMA_VERSION,
                "source": source,
                "signal_layer": signal_layer,
                "concepts": [concept for concept, _ in concepts],
                "mapping_source": "concept_mapping.json",
                "company_mapping_gate": company_mapping_gate,
                "suppression_reason": "generic_company_teaser_without_explicit_company" if generic_company_teaser else "",
                "important": bool(item.get("is_important")),
                "score": item.get("score"),
                "rank": item.get("rank"),
                "raw_quality": raw_quality,
                "quality_gate": _quality_gate(raw_quality),
                "positive_signal_quality": positive_quality,
                "event_extraction": _event_extraction_summary(extracted_events),
            },
            "source_count": 1,
            "graph_sync_status": "pending",
        }

    def _card_from_cls_item(self, item: Dict[str, Any], raw: Dict[str, Any], *, text: str) -> Dict[str, Any]:
        return self._card_from_dailynews_item(item, raw, text=text, source="cls_telegraph", raw_prefix="cls")

    def _match_concepts(self, text: str) -> List[Tuple[str, Dict[str, Any]]]:
        haystack = str(text or "")
        upper_haystack = haystack.upper()
        matched: List[Tuple[str, Dict[str, Any], int]] = []
        for concept, payload in self._concept_mapping.items():
            if not isinstance(payload, dict):
                continue
            aliases = [str(concept)]
            aliases.extend(str(item) for item in payload.get("aliases") or [] if str(item).strip())
            best = 0
            for alias in aliases:
                alias_text = str(alias or "").strip()
                if not alias_text:
                    continue
                target = upper_haystack if re.search(r"[A-Za-z]", alias_text) else haystack
                needle = alias_text.upper() if re.search(r"[A-Za-z]", alias_text) else alias_text
                if needle in target:
                    best = max(best, len(alias_text))
            if best:
                matched.append((str(concept), payload, best))
        matched.sort(key=lambda row: (-row[2], row[0]))
        return [(concept, payload) for concept, payload, _ in matched[:8]]


def news_signal_to_evidence_card(card: Dict[str, Any], *, symbol: str = "", name: str = "", run_id: str = "news-signal") -> EvidenceCard:
    companies = card.get("company_impacts") if isinstance(card.get("company_impacts"), list) else []
    selected_company = _select_company_for_stock(companies, symbol)
    stock_code = symbol or (selected_company.get("symbol") if selected_company else "")
    stock_name = name or (selected_company.get("name") if selected_company else "")
    if not stock_code:
        stock_code = "INDUSTRY"
        stock_name = ",".join(card.get("primary_industries") or []) or "产业级消息"

    signals: List[EvidenceSignal] = []
    for impact in card.get("industry_impacts") or []:
        if not isinstance(impact, dict):
            continue
        signals.append(
            EvidenceSignal(
                name=str(impact.get("industry") or "industry_impact"),
                value=impact.get("rationale") or card.get("summary_short"),
                direction=_evidence_direction(impact.get("direction")),
                strength=_strength_from_value(impact.get("strength")),
                score_delta=_score_delta_from_direction(impact.get("direction"), card.get("signal_score")),
                interpretation=str(impact.get("rationale") or ""),
            )
        )
    for impact in companies[:5]:
        if not isinstance(impact, dict):
            continue
        signals.append(
            EvidenceSignal(
                name=str(impact.get("name") or impact.get("symbol") or "company_impact"),
                value=impact.get("symbol"),
                direction=_evidence_direction(impact.get("direction")),
                strength=_strength_from_confidence(impact.get("confidence")),
                score_delta=_score_delta_from_direction(impact.get("direction"), card.get("signal_score")),
                interpretation=str(impact.get("rationale") or ""),
            )
        )
    confidence = _confidence_from_card(card)
    return EvidenceCard(
        card_id=str(card.get("card_id") or ""),
        run_id=run_id,
        stock=StockRef(code=str(stock_code), name=str(stock_name or ""), market="cn"),
        dimension="news_event",
        producer={"name": "news_signal_card", "schema_version": NEWS_SIGNAL_SCHEMA_VERSION},
        data_quality=DataQuality(
            status="ok" if card.get("status") == "active" else "stale",
            as_of=str(card.get("signal_date") or ""),
            freshness="intraday" if card.get("session") == "intraday" else "recent",
            source="news_signal_cards",
            source_chain=card.get("source_chain") or [],
            warnings=[] if card.get("mapping_status") == "mapped" else [f"mapping_status={card.get('mapping_status') or 'unknown'}"],
        ),
        signals=signals[:12],
        impact=EvidenceImpact(
            stance=_stance_from_impact(card.get("market_impact")),
            action_bias="wait" if card.get("evidence_grade") == "speculative" else "open",
            confidence=confidence,
            score_delta=max(-25.0, min(25.0, float(card.get("signal_score") or 0.0) / 4.0)),
            reason=str(card.get("summary_short") or ""),
        ),
        expiry=EvidenceExpiry(
            valid_until=str(card.get("valid_until") or ""),
            refresh_trigger=str(card.get("refresh_trigger") or ""),
            window=str(card.get("decay_rule") or ""),
        ),
        raw_ref=json.dumps(
            {
                "card_id": card.get("card_id"),
                "raw_episode_ids": card.get("raw_episode_ids") or [],
                "source_count": card.get("source_count"),
            },
            ensure_ascii=False,
            default=str,
        ),
    )


def _load_concept_mapping() -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(CONCEPT_MAPPING_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _resolve_target_date(value: str) -> date:
    parsed = _parse_date(value)
    if parsed:
        return parsed
    return get_effective_trading_date("cn")


def _normalize_stock_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        parts = [part for part in text.split(".") if part]
        for part in parts:
            if part.isdigit() and 5 <= len(part) <= 6:
                return part.zfill(6)
    digits = "".join(char for char in text if char.isdigit())
    if 5 <= len(digits) <= 6:
        return digits.zfill(6)
    return text


def _load_active_portfolio_holdings() -> List[Dict[str, Any]]:
    from sqlalchemy import select

    from src.storage import DatabaseManager, PortfolioAccount, PortfolioPosition

    holdings: Dict[str, Dict[str, Any]] = {}
    db = DatabaseManager.get_instance()
    with db.get_session() as session:
        rows = session.execute(
            select(
                PortfolioAccount.name,
                PortfolioPosition.symbol,
                PortfolioPosition.market,
                PortfolioPosition.currency,
                PortfolioPosition.quantity,
            )
            .join(PortfolioAccount, PortfolioPosition.account_id == PortfolioAccount.id)
            .where(PortfolioAccount.is_active == True)  # noqa: E712
            .where(PortfolioPosition.quantity > 0)
            .order_by(PortfolioPosition.symbol.asc(), PortfolioAccount.name.asc())
        ).all()

    for account_name, symbol, market, currency, quantity in rows:
        key = str(symbol or "").strip().upper()
        if not key:
            continue
        entry = holdings.setdefault(
            key,
            {
                "symbol": key,
                "market": str(market or "cn").strip().lower() or "cn",
                "currency": str(currency or "").strip() or "CNY",
                "accounts": [],
                "total_quantity": 0.0,
            },
        )
        account = str(account_name or "").strip()
        if account and account not in entry["accounts"]:
            entry["accounts"].append(account)
        try:
            entry["total_quantity"] += float(quantity or 0.0)
        except Exception:
            pass
    return list(holdings.values())


def _default_portfolio_anysearch_client() -> Optional[Any]:
    config = get_config()
    api_key = str(getattr(config, "anysearch_api_key", "") or "").strip()
    if not api_key:
        return None
    from src.search_service import AnySearchProvider

    return AnySearchProvider([api_key])


class _PortfolioStockNameResolver:
    def __init__(self) -> None:
        self._manager: Optional[Any] = None
        self._cache: Dict[str, str] = {}

    def __call__(self, symbol: str, market: str = "cn") -> str:
        del market
        key = str(symbol or "").strip().upper()
        if not key:
            return ""
        if key in self._cache:
            return self._cache[key]
        try:
            if self._manager is None:
                from data_provider import DataFetcherManager

                self._manager = DataFetcherManager()
            name = str(self._manager.get_stock_name(key, allow_realtime=False) or "").strip()
        except Exception:
            name = ""
        self._cache[key] = name
        return name


def _portfolio_anysearch_query(*, symbol: str, name: str, market: str) -> str:
    code = str(symbol or "").strip().upper()
    label = f"{str(name or '').strip()} {code}".strip()
    market_key = str(market or "").strip().lower()
    if market_key in {"us", "usa", "nasdaq", "nyse"} or re.fullmatch(r"[A-Z.]{1,8}", code):
        return f"{label} latest news earnings guidance risk"
    return f"{label} 最新消息 公告 业绩 风险"


def _run_portfolio_news_search(
    client: Any,
    *,
    query: str,
    max_results: int,
    max_age_days: int,
) -> Any:
    if hasattr(client, "search"):
        return client.search(query, max_results=max_results, days=max_age_days)
    return client.search_general_news(query, max_results=max_results, days=max_age_days)


PORTFOLIO_ANYSEARCH_ACTION_TERMS = (
    "业绩预增",
    "业绩预告",
    "业绩快报",
    "预计",
    "净利润",
    "净亏损",
    "亏损",
    "回购",
    "减持",
    "增持",
    "异常波动",
    "风险提示",
    "监管",
    "问询",
    "处罚",
    "立案",
    "调查",
    "诉讼",
    "仲裁",
    "中标",
    "订单",
    "合同",
    "签订",
    "签署",
    "框架协议",
    "战略合作",
    "送样",
    "布局",
    "批量供货",
    "量产供货",
    "建厂",
    "停产",
    "分拆",
    "上市事宜",
    "转股价格调整",
    "权益分派",
    "股权激励",
    "资产重组",
    "重大事项",
    "停牌",
    "复牌",
    "earnings",
    "guidance",
    "buyback",
    "repurchase",
    "offering",
    "investigation",
    "lawsuit",
    "risk",
)
PORTFOLIO_ANYSEARCH_POSITIVE_TERMS = (
    "业绩预增",
    "预增",
    "业绩快报",
    "盈利",
    "同比增长",
    "扭亏",
    "回购",
    "增持",
    "中标",
    "订单",
    "权益分派",
    "分红",
    "股权激励",
    "buyback",
    "repurchase",
)
PORTFOLIO_ANYSEARCH_NEGATIVE_TERMS = (
    "业绩预减",
    "预减",
    "业绩预亏",
    "预亏",
    "净亏损",
    "亏损",
    "同比下降",
    "减持",
    "风险提示",
    "监管",
    "问询",
    "处罚",
    "立案",
    "调查",
    "诉讼",
    "仲裁",
    "停牌",
    "guidance cut",
    "investigation",
    "lawsuit",
    "offering",
    "risk",
)
PORTFOLIO_ANYSEARCH_LIST_PAGE_TERMS = (
    "最新价格",
    "行情",
    "走势图",
    "公告大全",
    "公告列表",
    "公告摘要",
    "公司公告",
    "股票公告",
    "业绩公告",
    "历史业绩报告",
    "最新资讯",
    "个股资讯",
    "行情中心",
    "股票股价",
    "股价",
    "重大事项提醒与新闻公告",
)
PORTFOLIO_ANYSEARCH_LIST_URL_TERMS = (
    "corp/go.php/",
    "quote.eastmoney.com",
    "wap.eastmoney.com/quote",
    "xueqiu.com/s/",
    "stock.quote.stockstar.com/finance/performance",
    "assortment/stock/list/info/summary",
)


def _portfolio_anysearch_gate(
    result: Any,
    *,
    symbol: str,
    name: str,
    now: datetime,
    max_age_days: int,
) -> Dict[str, Any]:
    title = str(getattr(result, "title", "") or "").strip()
    snippet = str(getattr(result, "snippet", "") or "").strip()
    url = str(getattr(result, "url", "") or "").strip()
    source = str(getattr(result, "source", "") or "").strip()
    text = f"{title} {snippet} {source} {url}"
    lower_url = url.lower()
    if any(term in lower_url for term in PORTFOLIO_ANYSEARCH_LIST_URL_TERMS):
        return {"accepted": False, "reason": "list_or_quote_url"}
    upper_text = text.upper()
    code = str(symbol or "").strip().upper()
    stock_name = str(name or "").strip()
    has_anchor = bool(code and code in upper_text) or bool(stock_name and stock_name != code and stock_name in text)
    if not has_anchor:
        return {"accepted": False, "reason": "holding_anchor_missing"}

    has_action = any(term in text or term.lower() in text.lower() for term in PORTFOLIO_ANYSEARCH_ACTION_TERMS)
    title_has_action = any(term in title or term.lower() in title.lower() for term in PORTFOLIO_ANYSEARCH_ACTION_TERMS)
    title_is_list_page = any(term in title for term in PORTFOLIO_ANYSEARCH_LIST_PAGE_TERMS)
    is_list_page = any(term in text for term in PORTFOLIO_ANYSEARCH_LIST_PAGE_TERMS)
    if title_is_list_page and not title_has_action:
        return {"accepted": False, "reason": "list_or_quote_page"}
    if is_list_page and not has_action:
        return {"accepted": False, "reason": "list_or_quote_page"}
    if not has_action:
        return {"accepted": False, "reason": "actionable_terms_missing"}

    published_at = _parse_datetime(getattr(result, "published_date", None))
    if published_at is None:
        return {"accepted": False, "reason": "missing_published_date"}
    age_days = max(0, (now - published_at.replace(tzinfo=None)).days)
    if age_days > max_age_days:
        return {"accepted": False, "reason": "too_old", "published_at": published_at.isoformat()}
    return {
        "accepted": True,
        "reason": "actionable_holding_news",
        "published_at": published_at.isoformat(),
        "action_terms": [term for term in PORTFOLIO_ANYSEARCH_ACTION_TERMS if term in text or term.lower() in text.lower()][:5],
    }


def _portfolio_anysearch_dailynews_item(
    result: Any,
    *,
    symbol: str,
    name: str,
    market: str,
    holding: Dict[str, Any],
    query: str,
    rank: int,
    provider: str,
    gate: Dict[str, Any],
    now: datetime,
) -> Dict[str, Any]:
    published_at = _parse_datetime(gate.get("published_at")) or _parse_datetime(getattr(result, "published_date", None)) or now
    title = str(getattr(result, "title", "") or "").strip()
    snippet = str(getattr(result, "snippet", "") or "").strip()
    display_title = _portfolio_anysearch_display_title(
        title=title,
        snippet=snippet,
        symbol=symbol,
        name=name,
    )
    url = str(getattr(result, "url", "") or "").strip()
    stock = {
        "code": symbol,
        "symbol": symbol,
        "name": name or symbol,
        "market": market or holding.get("market") or "cn",
        "role": "portfolio_holding",
        "accounts": holding.get("accounts") or [],
        "total_quantity": holding.get("total_quantity") or 0.0,
    }
    return {
        "id": _stable_id("portfolio_anysearch", symbol, url, title),
        "title": display_title,
        "brief": display_title,
        "content": snippet,
        "snippet": snippet,
        "url": url,
        "published_at": published_at.isoformat(),
        "published_ts": int(published_at.timestamp()),
        "score": max(1.0, 1000.0 - float(rank)),
        "rank": rank,
        "is_important": True,
        "subjects": [],
        "subject_names": ["持仓消息面", "AnySearch"],
        "stocks": [stock],
        "polarity": _portfolio_anysearch_polarity(f"{display_title} {snippet}"),
        "provider": provider or "AnySearch",
        "source_query": query,
        "raw_gate": gate,
        "source_chain": [
            {
                "provider": provider or "AnySearch",
                "result": "ok",
                "scope": "portfolio_holdings",
                "source": str(getattr(result, "source", "") or ""),
                "url": url,
                "published_at": published_at.isoformat(),
                "rank": rank,
                "query": query,
            }
        ],
    }


def _portfolio_anysearch_polarity(text: str) -> str:
    value = str(text or "")
    lowered = value.lower()
    positive = any(term in value or term.lower() in lowered for term in PORTFOLIO_ANYSEARCH_POSITIVE_TERMS)
    negative = any(term in value or term.lower() in lowered for term in PORTFOLIO_ANYSEARCH_NEGATIVE_TERMS)
    if positive and negative:
        return "mixed"
    if positive:
        return "positive"
    if negative:
        return "negative"
    return "neutral"


def _portfolio_anysearch_display_title(
    *,
    title: str,
    snippet: str,
    symbol: str,
    name: str,
) -> str:
    cleaned_title = re.sub(r"\s+", " ", str(title or "")).strip()
    cleaned_snippet = re.sub(r"\s+", " ", str(snippet or "")).strip()
    label = str(name or symbol or "持仓").strip()
    if cleaned_title.startswith("证券代码") and "关于" in cleaned_snippet:
        start = cleaned_snippet.find("关于")
        end = cleaned_snippet.find("公告", start)
        if end > start:
            subject = cleaned_snippet[start : end + len("公告")]
            return _compact_text(f"{label}: {subject}", 180)
    return cleaned_title or _compact_text(cleaned_snippet, 180) or f"{label} 持仓消息面"


def _signal_date_and_session(value: datetime) -> Tuple[date, str]:
    local = value
    if local.hour < 9:
        return get_effective_trading_date("cn", current_time=local), "pre_open"
    if local.hour < 15 or (local.hour == 15 and local.minute == 0):
        return local.date(), "intraday"
    return local.date(), "post_close"


def _cjzc_theme_raw_episode(
    payload: Dict[str, Any],
    item: Dict[str, Any],
    *,
    target: date,
    published_at: datetime,
    ingested_at: datetime,
) -> Dict[str, Any]:
    theme = str(item.get("theme") or "未分类主题").strip()
    evidence = str(item.get("evidence") or payload.get("summary") or payload.get("title") or "").strip()
    keywords = _unique_strings(item.get("keywords") or [])
    title = _compact_text(f"{theme}: {evidence}" if evidence else f"{theme}: {payload.get('title') or '东方财富财经早餐'}", 220)
    content_parts = [evidence]
    if keywords:
        content_parts.append(f"关键词：{'、'.join(keywords[:8])}")
    related_boards = _unique_strings(item.get("related_boards") or [])
    if related_boards:
        content_parts.append(f"相关板块：{'、'.join(related_boards[:8])}")
    return {
        "episode_id": _stable_id("raw:cjzc", payload.get("matched_publish_date"), payload.get("link"), payload.get("title"), theme),
        "dedup_key": _stable_id("dedup:cjzc", payload.get("matched_publish_date"), payload.get("link"), theme, evidence),
        "source": "news_theme_daily",
        "provider": str(payload.get("source") or "akshare:stock_info_cjzc_em"),
        "source_id": f"{payload.get('matched_publish_date') or target.isoformat()}:{theme}",
        "url": str(payload.get("link") or ""),
        "title": title,
        "summary": evidence,
        "content": "\n".join(part for part in content_parts if part),
        "published_at": published_at,
        "ingested_at": ingested_at,
        "signal_date": target,
        "session": "pre_open",
        "subjects": _unique_strings([theme] + keywords),
        "stocks": item.get("mapped_stocks") or [],
        "source_chain": [{"provider": "get_eastmoney_cjzc_daily", "result": payload.get("status") or "ok"}],
        "raw_payload": {
            "parent_title": payload.get("title"),
            "parent_summary": payload.get("summary"),
            "parent_link": payload.get("link"),
            "matched_publish_date": payload.get("matched_publish_date"),
            "theme": item,
        },
        "status": payload.get("status") or "ok",
        "errors": payload.get("errors") or [],
    }


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value.strip():
        text = value.strip()
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                return datetime.strptime(text[: len(fmt)], fmt).date()
            except ValueError:
                continue
    return None


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(datetime.now().strftime(fmt))], fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except Exception:
        return None


def _stable_id(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]
    prefix = re.sub(r"[^a-zA-Z0-9:_-]", "", str(parts[0] or "id"))[:24]
    return f"{prefix}:{digest}"


def _compact_text(value: Any, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _attach_raw_quality(raw: Dict[str, Any], *, source: str, candidate_text: str = "") -> None:
    normalized = _normalize_news_content(
        raw.get("title"),
        raw.get("summary"),
        raw.get("content"),
        candidate_text,
    )
    quality = _assess_raw_news_quality(raw, normalized, source=source)
    raw["normalized_content"] = normalized
    raw["quality_score"] = quality["score"]
    raw["quality_grade"] = quality["grade"]
    raw["quality_flags"] = quality["flags"]
    raw["quality_status"] = quality["status"]
    if quality["status"] == "low_quality" and str(raw.get("status") or "ok") == "ok":
        raw["status"] = "low_quality"


def _normalize_news_content(*parts: Any) -> str:
    candidates: List[str] = []
    for part in parts:
        for text in _extract_text_fragments(part):
            cleaned = _clean_news_text(text)
            if cleaned:
                candidates.append(cleaned)
    result: List[str] = []
    seen: set[str] = set()
    for text in candidates:
        key = re.sub(r"\W+", "", text.lower())[:160]
        if not key or key in seen:
            continue
        seen.add(key)
        if any(_is_near_duplicate(text, existing) for existing in result):
            continue
        result.append(text)
    return "\n".join(result[:6]).strip()


def _extract_text_fragments(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        fragments: List[str] = []
        for key in ("title", "summary", "brief", "snippet", "content", "semantic_text", "text", "rationale"):
            if key in value:
                fragments.extend(_extract_text_fragments(value.get(key)))
        for key in ("subjects", "subject_names", "keywords"):
            if key in value:
                fragments.extend(_extract_text_fragments(value.get(key)))
        return fragments
    if isinstance(value, list):
        fragments = []
        for item in value[:12]:
            fragments.extend(_extract_text_fragments(item))
        return fragments
    text = str(value or "").strip()
    if not text:
        return []
    if text[:1] in {"{", "["}:
        try:
            loaded = json.loads(text)
            fragments = _extract_text_fragments(loaded)
            if fragments:
                return fragments
        except Exception:
            pass
    return [text]


def _clean_news_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\\n", "\n").replace("\\t", " ")
    text = re.sub(r"[\u200b\ufeff\xa0]+", " ", text)
    text = re.sub(r"(免责声明|风险提示)[:：].*$", "", text, flags=re.IGNORECASE)
    lines = [re.sub(r"\s+", " ", line).strip(" -\t") for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines).strip()


def _is_near_duplicate(text: str, existing: str) -> bool:
    if text == existing:
        return True
    if len(text) < 30 or len(existing) < 30:
        return text in existing or existing in text
    short, long = (text, existing) if len(text) < len(existing) else (existing, text)
    return short in long and len(short) / max(1, len(long)) > 0.72


def _assess_raw_news_quality(raw: Dict[str, Any], normalized: str, *, source: str) -> Dict[str, Any]:
    flags: List[str] = []
    source_text = str(source or raw.get("source") or "")
    score = 62.0
    if source_text == "news_theme_daily":
        score += 12.0
    elif source_text in {"cls_telegraph", "macro_finance"}:
        score += 8.0
    elif source_text == "xueqiu_hot":
        score += 2.0

    if raw.get("published_at"):
        score += 8.0
    else:
        score -= 15.0
        flags.append("missing_published_at")

    length = len(normalized)
    if length >= 160:
        score += 10.0
    elif length >= 60:
        score += 4.0
    elif length >= 25:
        score -= 12.0
        flags.append("thin_content")
    else:
        score -= 30.0
        flags.append("empty_or_tiny_content")

    content_text = _clean_news_text(raw.get("content") or "")
    summary_text = _clean_news_text(raw.get("summary") or "")
    if len(content_text) < 20 and len(summary_text) < 20:
        score -= 8.0
        flags.append("no_substantive_body")

    haystack = f"{raw.get('title') or ''} {normalized}"
    if any(term in haystack for term in MARKETING_TERMS):
        score -= 12.0
        flags.append("marketing_style")

    if any(term in haystack for term in SIGNAL_TERMS) or _macro_theme_for_text(haystack):
        score += 10.0
    else:
        score -= 8.0
        flags.append("weak_signal_terms")

    stocks = raw.get("stocks") if isinstance(raw.get("stocks"), list) else []
    subjects = raw.get("subjects") if isinstance(raw.get("subjects"), list) else []
    if stocks:
        score += 8.0
    elif subjects:
        score += 5.0
    elif _macro_theme_for_text(haystack):
        score += 6.0
    else:
        score -= 5.0
        flags.append("no_explicit_entity")

    if raw.get("errors"):
        score -= 20.0
        flags.append("source_errors")

    normalized_score = round(max(0.0, min(100.0, score)), 2)
    if normalized_score >= 75:
        grade = "high"
    elif normalized_score >= 55:
        grade = "medium"
    else:
        grade = "low"
    status = "low_quality" if normalized_score < 45 else "ok"
    return {
        "score": normalized_score,
        "grade": grade,
        "flags": flags,
        "status": status,
    }


def _raw_quality_summary(raw: Dict[str, Any]) -> Dict[str, Any]:
    score = _float_or_none(raw.get("quality_score"))
    flags = raw.get("quality_flags") if isinstance(raw.get("quality_flags"), list) else []
    return {
        "score": round(float(score or 0.0), 2),
        "grade": str(raw.get("quality_grade") or "unknown"),
        "status": str(raw.get("quality_status") or ("low_quality" if raw.get("status") == "low_quality" else "ok")),
        "flags": flags,
        "normalized_length": len(str(raw.get("normalized_content") or "")),
    }


def _status_from_quality(raw_quality: Dict[str, Any]) -> str:
    return "low_quality" if str(raw_quality.get("status") or "") == "low_quality" else "active"


def _quality_gate(raw_quality: Dict[str, Any]) -> Dict[str, Any]:
    status = _status_from_quality(raw_quality)
    return {
        "status": status,
        "reason": "raw_news_quality_below_threshold" if status == "low_quality" else "passed",
        "score_threshold": 45,
    }


def _score_with_quality(score: float, raw_quality: Dict[str, Any]) -> float:
    quality_score = float(raw_quality.get("score") or 0.0)
    adjusted = float(score or 0.0)
    if quality_score < 45:
        adjusted = min(adjusted * 0.45, 35.0)
    elif quality_score < 55:
        adjusted *= 0.72
    elif quality_score < 70:
        adjusted *= 0.9
    return round(max(0.0, min(100.0, adjusted)), 2)


def _unique_strings(values: Iterable[Any]) -> List[str]:
    result: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _tone_from_polarity(value: Any) -> str:
    text = str(value or "").lower()
    if text in {"positive", "bullish"}:
        return "positive"
    if text in {"negative", "bearish", "deny_or_clarification"}:
        return "negative"
    if text == "mixed":
        return "mixed"
    return "neutral"


def _infer_tone(text: str) -> str:
    value = str(text or "")
    positive = sum(1 for term in POSITIVE_TERMS if term in value)
    negative = sum(1 for term in NEGATIVE_TERMS if term in value)
    if positive and negative:
        return "mixed"
    if positive:
        return "positive"
    if negative:
        return "negative"
    return "neutral"


def _market_impact_from_tone(tone: str) -> str:
    if tone in {"positive", "negative", "mixed"}:
        return tone
    return "unknown"


def _positive_signal_quality(text: str) -> Dict[str, Any]:
    value = str(text or "")
    lowered = value.lower()

    def has_any(terms: Iterable[str]) -> bool:
        return any(str(term).lower() in lowered for term in terms if str(term or "").strip())

    matched_rules: List[str] = []
    score_adjustment = 0.0
    tone_override = ""
    evidence_grade_override = ""

    unlock_terms = ("解禁", "限售股上市", "解除限售", "大股东解禁", "控股股东解禁")
    earnings_terms = ("业绩预增", "预增", "业绩预告", "净利润同比增长", "预计净利润", "预计202")
    if has_any(unlock_terms) and has_any(earnings_terms):
        matched_rules.append("大股东解禁前突发业绩预增")
        return {
            "category": "risk_disguised_positive",
            "label": "利空式利好",
            "strength": "weak",
            "matched_rules": matched_rules,
            "tone_override": "negative",
            "evidence_grade_override": "speculative",
            "score_cap": 45.0,
            "score_adjustment": -18.0,
        }

    foreign_terms = (
        "国外",
        "海外",
        "国际",
        "境外",
        "美国",
        "欧洲",
        "日本",
        "韩国",
        "英伟达",
        "nvidia",
        "苹果",
        "apple",
        "特斯拉",
        "tesla",
        "微软",
        "microsoft",
        "亚马逊",
        "amazon",
        "谷歌",
        "google",
        "meta",
        "台积电",
        "tsmc",
        "三星",
        "samsung",
    )
    contract_terms = ("签订", "签署", "合同", "订单", "供货协议", "采购协议", "长单")
    if has_any(foreign_terms) and has_any(contract_terms):
        matched_rules.append("已与国外大厂签合同")
    if re.search(r"(落实|拟|计划|投资|建设).{0,24}(?:\d+(?:\.\d+)?\s*)?(?:亿美金|亿美元|美元).{0,24}(建厂|工厂|基地|产能)", value, re.IGNORECASE):
        matched_rules.append("落实大额美元建厂")
    if has_any(("海外停产", "国外停产", "境外停产", "海外工厂停产", "海外减产", "海外停工")):
        matched_rules.append("海外停产")
    if "增持" in value and "减持" not in value:
        matched_rules.append("增持")
    if _is_company_share_buyback_true_positive(value):
        matched_rules.append("公司股份回购")
    if has_any(("批量供货", "量产供货", "批量交付", "规模化供货", "批量出货", "稳定供货")):
        matched_rules.append("批量供货")
    if matched_rules:
        return {
            "category": "true_positive",
            "label": "真利好",
            "strength": "strong",
            "matched_rules": matched_rules[:4],
            "tone_override": "positive",
            "evidence_grade_override": "",
            "score_cap": None,
            "score_adjustment": 8.0,
        }

    if has_any(("框架协议", "意向协议", "合作备忘录", "战略合作协议")):
        matched_rules.append("随时可撤的框架/意向协议")
    if has_any(("正布局", "正在布局", "布局", "正送样", "送样", "样品", "客户验证")):
        matched_rules.append("布局/送样/客户验证")
    if "新增概念" in value or ("新增" in value and "概念" in value) or has_any(("概念股", "概念业务")):
        matched_rules.append("新增概念")
    if re.search(r"(开展|推进|启动).{0,12}(研究|研发|试验|实验)", value):
        matched_rules.append("新增研究/研发")
    domestic_terms = ("国内", "境内", "本土")
    if has_any(contract_terms) and not has_any(foreign_terms) and (has_any(domestic_terms) or "合同" in value):
        matched_rules.append("国内主体合同")
    if matched_rules:
        score_adjustment = -12.0
        tone_override = "positive"
        evidence_grade_override = "speculative"
        return {
            "category": "one_day_positive",
            "label": "一日游式利好",
            "strength": "weak",
            "matched_rules": _unique_strings(matched_rules)[:4],
            "tone_override": tone_override,
            "evidence_grade_override": evidence_grade_override,
            "score_cap": 49.0,
            "score_adjustment": score_adjustment,
        }

    return {
        "category": "unclassified",
        "label": "未识别",
        "strength": "unknown",
        "matched_rules": [],
        "tone_override": "",
        "evidence_grade_override": "",
        "score_cap": None,
        "score_adjustment": 0.0,
    }


def _is_company_share_buyback_true_positive(text: str) -> bool:
    value = str(text or "")
    if not value:
        return False
    lowered = value.lower()
    macro_repo_terms = (
        "逆回购",
        "正回购",
        "央行",
        "人民银行",
        "公开市场",
        "回购操作",
        "回购利率",
    )
    if any(term.lower() in lowered for term in macro_repo_terms):
        return False
    buyback_terms = (
        "股份回购",
        "回购股份",
        "回购公司股份",
        "回购部分股份",
        "回购a股股份",
        "回购A股股份",
        "以集中竞价方式回购",
    )
    if not any(term.lower() in lowered for term in buyback_terms):
        return False
    company_context_terms = (
        "董事长",
        "控股股东",
        "实际控制人",
        "公司",
        "公告",
        ".sh",
        ".sz",
        ".bj",
    )
    shareholder_return_terms = (
        "用于注销",
        "全部用于注销",
        "减少注册资本",
        "注销并减少注册资本",
        "提议回购",
    )
    return any(term.lower() in lowered for term in company_context_terms) or any(
        term.lower() in lowered for term in shareholder_return_terms
    )


def _apply_positive_signal_quality_score(score: float, quality: Dict[str, Any]) -> float:
    adjusted = float(score or 0.0) + float(quality.get("score_adjustment") or 0.0)
    cap = quality.get("score_cap")
    if cap is not None:
        adjusted = min(adjusted, float(cap))
    return round(max(0.0, min(100.0, adjusted)), 2)


def _macro_theme_for_text(text: str) -> str:
    value = str(text or "")
    lowered = value.lower()
    if not any(_text_has_macro_term(value, lowered, str(term)) for term in MACRO_TERMS):
        return ""
    if any(_text_has_macro_term(value, lowered, term) for term in ("非农", "就业数据", "失业率", "美联储", "FOMC", "美元指数")):
        return "海外宏观"
    if any(_text_has_macro_term(value, lowered, term) for term in CENTRAL_BANK_OPERATION_TERMS):
        return "国内流动性"
    if any(_text_has_macro_term(value, lowered, term) for term in ("CPI", "PPI", "PMI", "GDP", "PCE", "M2", "通胀", "社融")):
        return "宏观经济"
    if any(_text_has_macro_term(value, lowered, term) for term in ("油价", "原油", "大宗商品")):
        return "大宗商品"
    return "宏观市场"


def _text_has_macro_term(text: str, lowered: str, term: str) -> bool:
    term_text = str(term or "").strip()
    if not term_text:
        return False
    if term_text in CENTRAL_BANK_ENTITY_TERMS:
        return term_text in text and any(_text_has_macro_term(text, lowered, item) for item in CENTRAL_BANK_OPERATION_TERMS)
    if term_text.upper() in ASCII_MACRO_TERMS:
        return bool(re.search(rf"(?<![A-Za-z0-9.]){re.escape(term_text)}(?![A-Za-z0-9.])", text, re.IGNORECASE))
    return term_text.lower() in lowered


def _classify_signal_layer(
    *,
    text: str,
    company_impacts: List[Dict[str, Any]],
    explicit_company_impacts: List[Dict[str, Any]],
    primary_industries: List[str],
) -> str:
    if _macro_theme_for_text(text):
        return "macro"
    if explicit_company_impacts or company_impacts:
        return "company"
    if primary_industries:
        return "industry"
    return "industry"


def _horizon_for_text(text: str, valid_from: datetime) -> Tuple[str, str, datetime]:
    value = str(text or "")
    if any(term in value for term in LONG_TERMS):
        return "long", "quarterly", valid_from + timedelta(days=90)
    if any(term in value for term in MEDIUM_TERMS):
        return "medium", "2w", valid_from + timedelta(days=14)
    return "short", "3d", valid_from + timedelta(days=3)


def _refresh_trigger_for_horizon(horizon: str) -> str:
    if horizon == "long":
        return "财报验证、产业政策细则或技术路线变化"
    if horizon == "medium":
        return "订单落地、涨价函、产能或客户验证"
    return "盘面资金验证、公司澄清或后续快讯"


def _company_impacts_from_mapped_stocks(stocks: Any, tone: str) -> Tuple[List[Dict[str, Any]], str, float]:
    if not isinstance(stocks, list) or not stocks:
        return [], "industry_only", 0.35
    impacts: List[Dict[str, Any]] = []
    for stock in stocks[:8]:
        if not isinstance(stock, dict):
            continue
        code = str(stock.get("code") or stock.get("symbol") or stock.get("ts_code") or "").strip()
        name = str(stock.get("name") or stock.get("stock_name") or code).strip()
        if not code and not name:
            continue
        impacts.append(
            {
                "symbol": code,
                "name": name,
                "direction": _direction_from_tone(tone),
                "confidence": 0.78 if code else 0.52,
                "mapping_status": "mapped" if code else "ambiguous",
                "role": stock.get("role") or "",
                "rationale": f"来自主题词典映射：{stock.get('role') or '产业链相关公司'}",
            }
        )
    if not impacts:
        return [], "industry_only", 0.35
    if any(item.get("mapping_status") == "ambiguous" for item in impacts):
        return impacts, "ambiguous", 0.55
    return impacts, "mapped", 0.78


def _stocks_explicitly_mentioned(stocks: Any, text: str) -> List[Dict[str, Any]]:
    if not isinstance(stocks, list) or not stocks:
        return []
    haystack = str(text or "").upper()
    accepted: List[Dict[str, Any]] = []
    for stock in stocks:
        if not isinstance(stock, dict):
            continue
        code = str(stock.get("code") or stock.get("symbol") or stock.get("ts_code") or "").strip().upper()
        name = str(stock.get("name") or stock.get("stock_name") or "").strip()
        code_variants = {code, code.split(".")[0]} if code else set()
        code_mentioned = any(value and value in haystack for value in code_variants)
        name_mentioned = len(name) >= 2 and name.upper() in haystack
        if code_mentioned or name_mentioned:
            accepted.append(stock)
    return accepted


def _is_unresolved_company_teaser(text: str, company_impacts: Any) -> bool:
    if isinstance(company_impacts, list) and company_impacts:
        return False
    normalized = str(text or "")
    return any(term in normalized for term in GENERIC_COMPANY_TEASER_TERMS)


def _company_mapping_gate(*, candidate_count: int, accepted_count: int) -> Dict[str, Any]:
    if accepted_count > 0:
        status = "explicit_company_only"
    elif candidate_count > 0:
        status = "blocked_no_explicit_company"
    else:
        status = "no_company_candidates"
    return {
        "status": status,
        "candidate_count": int(candidate_count or 0),
        "accepted_count": int(accepted_count or 0),
        "dropped_count": max(0, int(candidate_count or 0) - int(accepted_count or 0)),
        "rule": "company_name_or_stock_code_must_appear_in_news_text",
    }


def _company_impacts_from_cls_stocks(stocks: Any, tone: str) -> Tuple[List[Dict[str, Any]], str, float]:
    if not isinstance(stocks, list) or not stocks:
        return [], "industry_only", 0.35
    impacts: List[Dict[str, Any]] = []
    for stock in stocks[:8]:
        if not isinstance(stock, dict):
            continue
        code = str(stock.get("code") or stock.get("ts_code") or "").strip()
        name = str(stock.get("name") or code).strip()
        if not code and not name:
            continue
        impacts.append(
            {
                "symbol": code,
                "name": name,
                "direction": _direction_from_tone(tone),
                "confidence": 0.9 if code else 0.45,
                "mapping_status": "mapped" if code else "ambiguous",
                "role": "CLS explicit stock_list",
                "rationale": "财联社快讯显式关联股票",
            }
        )
    if not impacts:
        return [], "industry_only", 0.35
    if any(item.get("mapping_status") == "ambiguous" for item in impacts):
        return impacts, "ambiguous", 0.55
    return impacts, "mapped", 0.9


def _merge_company_impacts(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for item in items:
        key = str(item.get("symbol") or item.get("name") or "").strip()
        if not key:
            continue
        previous = merged.get(key)
        if previous is None or float(item.get("confidence") or 0.0) > float(previous.get("confidence") or 0.0):
            merged[key] = item
    return list(merged.values())[:8]


def _combine_mapping_status(explicit_status: str, explicit_confidence: float, mapped_status: str, mapped_confidence: float, *, has_industry: bool) -> Tuple[str, float]:
    statuses = {explicit_status, mapped_status}
    confidence = max(float(explicit_confidence or 0.0), float(mapped_confidence or 0.0))
    if "mapped" in statuses:
        return "mapped", confidence
    if "ambiguous" in statuses:
        return "ambiguous", confidence
    if has_industry:
        return "industry_only", max(confidence, 0.35)
    return "unmapped", 0.0


def _direction_from_tone(tone: str) -> str:
    if tone == "positive":
        return "benefit"
    if tone == "negative":
        return "harm"
    if tone == "mixed":
        return "uncertain"
    return "neutral"


def _industry_impact(industry: str, tone: str, rationale: str, *, strength: str) -> Dict[str, Any]:
    return {
        "industry": industry,
        "direction": _direction_from_tone(tone),
        "strength": strength,
        "rationale": _compact_text(rationale, 220),
    }


def _extract_news_events(
    *,
    raw: Dict[str, Any],
    card_id: str,
    primary_theme: str,
    text: str,
    related_boards: List[str],
    company_impacts: List[Dict[str, Any]],
    tone: str,
    mapping_confidence: float,
    subject_names: Optional[List[str]] = None,
    llm_extractor: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    evidence = _primary_evidence_sentence(text)
    event_type = _event_category_for_text(evidence or text)
    trigger = _event_trigger_for_text(event_type, evidence or text)
    metric_value = _metric_value_for_text(evidence or text)
    source_quality = _float_or_none(raw.get("quality_score"))
    confidence = _event_confidence(
        source_quality=source_quality,
        mapping_confidence=mapping_confidence,
        evidence=evidence,
        entity_count=len(company_impacts) + len(related_boards),
    )
    entity_links = _event_entity_links(
        primary_theme=primary_theme,
        related_boards=related_boards,
        company_impacts=company_impacts,
        subject_names=subject_names or [],
    )
    verification_sources = _event_verification_sources(raw)
    verification_status = "source_verified" if verification_sources and confidence >= 0.72 else "source_only"
    event_time = raw.get("published_at") if isinstance(raw.get("published_at"), datetime) else _parse_datetime(raw.get("published_at"))
    fallback_events = [
        {
            "event_id": _stable_id("event", raw.get("episode_id"), card_id, event_type, trigger, evidence),
            "raw_episode_id": str(raw.get("episode_id") or ""),
            "card_id": card_id,
            "signal_date": raw.get("signal_date"),
            "event_time": event_time,
            "event_type": event_type,
            "trigger": trigger,
            "subject": primary_theme or _compact_text(text, 80),
            "object": _event_object_for_text(evidence or text, related_boards, company_impacts),
            "direction": _direction_from_tone(tone),
            "metric_value": metric_value,
            "evidence_sentence": evidence,
            "source_url": str(raw.get("url") or ""),
            "source": str(raw.get("source") or ""),
            "extractor": "rule_fallback",
            "confidence": confidence,
            "verification_status": verification_status,
            "verification_sources": verification_sources,
            "entity_links": entity_links,
            "diagnostics": {
                "schema_version": "news_extracted_event.v1",
                "extractor_role": "fallback_not_primary",
                "keyword_dependency": "reduced_to_trigger_hint",
                "source_quality_score": source_quality,
                "mapping_confidence": mapping_confidence,
                "raw_quality_grade": raw.get("quality_grade"),
            },
            "status": "active",
        }
    ]
    if llm_extractor is None:
        _annotate_event_llm_diagnostics(fallback_events, {"status": "disabled", "reason": "no_extractor"})
        return fallback_events

    context = {
        "raw": raw,
        "card_id": card_id,
        "primary_theme": primary_theme,
        "text": text,
        "related_boards": related_boards,
        "company_impacts": company_impacts,
        "tone": tone,
        "mapping_confidence": mapping_confidence,
        "subject_names": subject_names or [],
    }
    try:
        llm_events, llm_diagnostics = llm_extractor.extract(context, fallback_events)
    except Exception as exc:
        llm_events = []
        llm_diagnostics = {
            "status": "failed",
            "reason": exc.__class__.__name__,
            "message": _compact_text(str(exc), 240),
        }
    if llm_events:
        return llm_events
    _annotate_event_llm_diagnostics(fallback_events, llm_diagnostics)
    return fallback_events


def _news_event_extractor_system_prompt() -> str:
    return (
        "你是财经新闻事件事实抽取器。只抽取原文明确表达的事件事实，"
        "不要做股票推荐，不要推断未出现的公司，不要输出行情预测。"
        "返回一个 JSON object，顶层只能包含 events 数组。"
        "每个 event 最多包含以下字段：event_type, trigger, subject, object, direction, "
        "metric_value, evidence_sentence, entity_links, confidence, verification_status。"
        "event_type 必须从以下枚举选择：价格/供需、大客户/订单、技术突破、供应链/替代、政策/宏观、业绩验证、产能变化、其他。"
        "direction 必须是 benefit/harm/neutral/uncertain。"
        "verification_status 必须是 source_verified/source_only/unverified。"
        "evidence_sentence 必须是原文中可核验的短句。不要包含 Markdown 或解释。"
    )


def _news_event_extractor_user_prompt(context: Dict[str, Any]) -> str:
    raw = context.get("raw") if isinstance(context.get("raw"), dict) else {}
    company_impacts = []
    for item in context.get("company_impacts") or []:
        if not isinstance(item, dict):
            continue
        company_impacts.append(
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "direction": item.get("direction"),
                "confidence": item.get("confidence"),
                "mapping_status": item.get("mapping_status"),
                "role": item.get("role"),
            }
        )
    payload = {
        "source": raw.get("source"),
        "provider": raw.get("provider"),
        "published_at": _serialize_for_json(raw.get("published_at")),
        "signal_date": _serialize_for_json(raw.get("signal_date")),
        "title": _compact_text(raw.get("title"), 220),
        "summary": _compact_text(raw.get("summary"), 320),
        "primary_theme": context.get("primary_theme"),
        "related_boards": context.get("related_boards") or [],
        "subject_names": context.get("subject_names") or [],
        "known_company_impacts": company_impacts[:8],
        "news_text": _compact_text(context.get("text"), 2600),
    }
    return (
        "请抽取最多 3 个最关键的事件事实。"
        "如果新闻只是泛泛讨论或缺少明确事件，返回 {\"events\": []}。"
        "不要因为 known_company_impacts 存在就编造公司事件；它只用于实体消歧。"
        "\n\n输入：\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _parse_json_object(text: str) -> Dict[str, Any]:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(cleaned[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM response is not a JSON object")
    return parsed


def _extract_litellm_content(response: Any) -> str:
    choices = response.get("choices") if isinstance(response, dict) else getattr(response, "choices", None)
    if not choices:
        raise ValueError("LLM returned no choices")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else getattr(choice, "message", None)
    content: Any = None
    if isinstance(message, dict):
        content = message.get("content")
    elif message is not None:
        content = getattr(message, "content", None)
    if content is None and isinstance(choice, dict):
        content = choice.get("text")
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        content = "".join(parts)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM returned empty content")
    return content.strip()


def _normalize_litellm_usage(usage_obj: Any) -> Dict[str, Any]:
    if not usage_obj:
        return {}

    def _get_value(key: str) -> int:
        if isinstance(usage_obj, dict):
            return int(usage_obj.get(key) or 0)
        return int(getattr(usage_obj, key, 0) or 0)

    return {
        "prompt_tokens": _get_value("prompt_tokens"),
        "completion_tokens": _get_value("completion_tokens"),
        "total_tokens": _get_value("total_tokens"),
    }


def _normalize_llm_entity_links(value: Any, fallback: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    links: List[Dict[str, Any]] = []
    if isinstance(value, list):
        for item in value[:12]:
            if not isinstance(item, dict):
                continue
            name = _compact_text(item.get("name"), 80)
            if not name:
                continue
            entity_type = str(item.get("entity_type") or item.get("type") or "entity").strip()
            if entity_type not in {"company", "industry", "macro", "entity", "person", "country", "policy"}:
                entity_type = "entity"
            link: Dict[str, Any] = {
                "entity_type": entity_type,
                "name": name,
                "confidence": round(_bounded_float(item.get("confidence"), default=0.6, minimum=0.0, maximum=0.98), 3),
                "source": _compact_text(item.get("source") or "llm_json", 60),
            }
            symbol = _compact_text(item.get("symbol"), 24)
            if symbol:
                link["symbol"] = symbol
            links.append(link)
    if not links:
        links = [dict(item) for item in fallback if isinstance(item, dict)]
    deduped: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str]] = set()
    for link in links:
        key = (str(link.get("entity_type") or ""), str(link.get("name") or ""), str(link.get("symbol") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(link)
    return deduped[:12]


def _annotate_event_llm_diagnostics(events: List[Dict[str, Any]], diagnostics: Optional[Dict[str, Any]]) -> None:
    if not diagnostics:
        return
    for event in events:
        if not isinstance(event, dict):
            continue
        event_diagnostics = event.setdefault("diagnostics", {})
        if isinstance(event_diagnostics, dict):
            event_diagnostics["llm_extraction"] = diagnostics


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    parsed = _float_or_none(value)
    if parsed is None:
        parsed = default
    return max(minimum, min(maximum, float(parsed)))


def _serialize_for_json(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _event_extraction_summary(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "schema_version": "news_extracted_event.v1",
        "event_count": len(events),
        "extractors": _count_dict(item.get("extractor") for item in events),
        "verification_counts": _count_dict(item.get("verification_status") for item in events),
        "event_types": _count_dict(item.get("event_type") for item in events),
    }


def _primary_evidence_sentence(text: str) -> str:
    snippets = _evidence_snippets(text)
    if snippets:
        return snippets[0]
    return _compact_text(text, 180)


def _event_trigger_for_text(event_type: str, text: str) -> str:
    haystack = str(text or "")
    trigger_groups = {
        "价格/供需": ("提价", "涨价", "价格上涨", "售价", "均价", "合约价格", "合同价格", "ASP", "报价"),
        "大客户/订单": ("量产", "投产", "交付", "出货", "供货", "订单", "定点", "认证", "客户导入"),
        "技术突破": ("突破", "路线", "工艺", "技术", "专利", "良率", "验证"),
        "供应链/替代": ("制裁", "禁令", "封锁", "限制", "关税", "出口管制", "供应受限", "断供", "国产替代", "进口替代", "自主可控", "二供", "出口"),
        "政策/宏观": ("政策", "规划", "征求意见", "补贴", "审评审批", "降准", "降息", "逆回购", "MLF", "LPR"),
        "业绩验证": ("净利润", "业绩", "预告", "同比", "环比", "盈利"),
        "产能变化": ("扩产", "产能", "建厂", "项目", "投建"),
    }
    for term in trigger_groups.get(event_type, ()):
        if term in haystack:
            return term
    return event_type


def _metric_value_for_text(text: str) -> str:
    matches = re.findall(r"\d+(?:\.\d+)?\s*(?:%|亿元|亿|万元|万|元|美元|韩元|日元|套|台|吨|GWh|MW|GW)", str(text or ""))
    return "、".join(matches[:4])


def _event_object_for_text(text: str, related_boards: List[str], company_impacts: List[Dict[str, Any]]) -> str:
    company_names = [item.get("name") or item.get("symbol") for item in company_impacts[:3] if isinstance(item, dict)]
    targets = _unique_strings(company_names + related_boards[:3])
    if targets:
        return "、".join(targets[:5])
    return _compact_text(text, 120)


def _event_confidence(*, source_quality: Optional[float], mapping_confidence: float, evidence: str, entity_count: int) -> float:
    quality = float(source_quality if source_quality is not None else 65.0)
    score = 0.28 + min(0.35, quality / 100.0 * 0.35)
    score += min(0.22, max(0.0, float(mapping_confidence or 0.0)) * 0.22)
    if evidence:
        score += 0.08
    if _metric_value_for_text(evidence):
        score += 0.04
    if entity_count:
        score += min(0.08, entity_count * 0.015)
    return round(max(0.0, min(0.98, score)), 3)


def _event_entity_links(
    *,
    primary_theme: str,
    related_boards: List[str],
    company_impacts: List[Dict[str, Any]],
    subject_names: List[str],
) -> List[Dict[str, Any]]:
    links: List[Dict[str, Any]] = []
    if primary_theme:
        links.append({"entity_type": "industry", "name": primary_theme, "confidence": 0.72, "source": "primary_theme"})
    for board in _unique_strings(related_boards)[:6]:
        links.append({"entity_type": "industry", "name": board, "confidence": 0.58, "source": "related_board"})
    for subject in _unique_strings(subject_names)[:6]:
        if subject and subject != primary_theme:
            links.append({"entity_type": "entity", "name": subject, "confidence": 0.55, "source": "source_subject"})
    for company in company_impacts[:8]:
        if not isinstance(company, dict):
            continue
        name = str(company.get("name") or company.get("symbol") or "").strip()
        if not name:
            continue
        links.append(
            {
                "entity_type": "company",
                "name": name,
                "symbol": company.get("symbol"),
                "confidence": round(float(_float_or_none(company.get("confidence")) or 0.0), 3),
                "source": str(company.get("role") or company.get("mapping_status") or "company_impact"),
            }
        )
    deduped: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str]] = set()
    for link in links:
        key = (str(link.get("entity_type") or ""), str(link.get("name") or ""), str(link.get("symbol") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(link)
    return deduped


def _event_verification_sources(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    url = str(raw.get("url") or "").strip()
    if url:
        result.append({"source": str(raw.get("source") or ""), "url": url, "status": "source_reported"})
    for item in raw.get("source_chain") or []:
        if not isinstance(item, dict):
            continue
        endpoint = str(item.get("endpoint") or item.get("page_url") or "").strip()
        provider = str(item.get("provider") or raw.get("provider") or raw.get("source") or "")
        if endpoint and not any(existing.get("url") == endpoint for existing in result):
            result.append({"source": provider, "url": endpoint, "status": str(item.get("result") or "source_reported")})
    return result[:4]


def _collect_extracted_events(cards: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for card in cards:
        for event in card.get("extracted_events") or []:
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("event_id") or "")
            if not event_id or event_id in seen:
                continue
            seen.add(event_id)
            events.append(event)
    return events


def _transmission_paths(
    theme: str,
    related_boards: List[str],
    company_impacts: List[Dict[str, Any]],
    rationale: str,
    *,
    extracted_events: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    if not theme:
        return []
    primary_event = (extracted_events or [{}])[0] if extracted_events else {}
    category = str(primary_event.get("event_type") or _event_category_for_text(rationale))
    template = _supply_chain_template_for_text(rationale)
    if template:
        category = "供应链/替代"
    catalyst_score = _catalyst_score(category, rationale)
    transmission_score = _transmission_score(theme, related_boards, company_impacts)
    mapping_score = _mapping_score(company_impacts)
    timeliness_score = 18.0
    event_score = round(catalyst_score + transmission_score + mapping_score + timeliness_score, 1)
    primary_targets = _unique_strings(
        [item.get("name") or item.get("symbol") for item in company_impacts[:4] if isinstance(item, dict)]
        or related_boards[:4]
        or [theme]
    )
    mechanism = _chain_mechanism(category, theme, related_boards, company_impacts, rationale)
    chain_steps = [
        {
            "label": category,
            "text": _compact_text(rationale, 100),
            "score": round(catalyst_score, 1),
        },
        {
            "label": "传导机制",
            "text": mechanism,
            "score": round(transmission_score, 1),
        },
        {
            "label": "映射落点",
            "text": _mapping_step_text(company_impacts, related_boards, theme),
            "score": round(mapping_score, 1),
        },
    ]
    if template:
        chain_steps = _supply_chain_template_steps(template, company_impacts, related_boards, theme, mapping_score)
        transmission_score = min(24.0, transmission_score + 3.0)
        event_score = round(catalyst_score + transmission_score + mapping_score + timeliness_score, 1)
        mechanism = template["mechanism"]
    conclusion = _chain_conclusion(category, theme, primary_targets, event_score)
    path = {
            "source": theme,
            "event_id": primary_event.get("event_id"),
            "event_category": category,
            "event_score": event_score,
            "mechanism": mechanism,
            "target": ",".join(related_boards[:3]) or ",".join(item.get("name") or item.get("symbol") or "" for item in company_impacts[:3]) or theme,
            "affected_industries": [theme] + related_boards[:5],
            "affected_symbols": [item.get("symbol") for item in company_impacts if item.get("symbol")][:6],
            "inference_level": "first_order" if company_impacts else "explicit",
            "evidence_grade": "plausible",
            "verification_status": primary_event.get("verification_status") or "source_only",
            "event_confidence": primary_event.get("confidence"),
            "chain_steps": chain_steps,
            "score_breakdown": {
                "catalyst": round(catalyst_score, 1),
                "transmission": round(transmission_score, 1),
                "mapping": round(mapping_score, 1),
                "timeliness": timeliness_score,
                "total": event_score,
            },
            "evidence_snippets": _evidence_snippets(rationale),
            "conclusion": conclusion,
            "rationale": _compact_text(rationale, 220),
        }
    if template:
        path["evidence_template"] = template
        path["template_id"] = template["template_id"]
    return [path]


def _supply_chain_template_for_text(text: str) -> Optional[Dict[str, Any]]:
    haystack = str(text or "")
    if not haystack:
        return None
    foreign_terms = (
        "海外",
        "国外",
        "美国",
        "欧洲",
        "日本",
        "韩国",
        "日韩",
        "欧美",
        "台厂",
        "海外大厂",
        "国际大厂",
        "出口",
        "进口",
    )
    disruption_terms = (
        "限制",
        "管制",
        "禁令",
        "制裁",
        "封锁",
        "关税",
        "断供",
        "供应受限",
        "停产",
        "涨价",
        "提价",
        "扩产",
        "订单",
        "认证",
        "客户导入",
    )
    substitution_terms = (
        "国产替代",
        "进口替代",
        "自主可控",
        "本土替代",
        "国产化",
        "二供",
        "第二供应商",
        "卡脖子",
        "替代窗口",
    )
    has_foreign_signal = any(term in haystack for term in foreign_terms) and any(term in haystack for term in disruption_terms)
    has_substitution_signal = any(term in haystack for term in substitution_terms)
    if has_foreign_signal and has_substitution_signal:
        template_id = "foreign_supply_to_domestic_substitution"
        mechanism = "海外供给、出口或客户变化先改变进口链条约束，再验证国内替代产品、二供认证、材料设备或模组封测承接能力。"
        required_evidence = ["海外限制/提价/扩产/订单变化", "国内可替代环节", "客户认证或二供资格", "公司产品或产能证据"]
    elif has_substitution_signal:
        template_id = "domestic_substitution_policy"
        mechanism = "国产替代或自主可控信号需要落到明确产品环节，再用政策、客户验证和公司产品证据确认可承接份额。"
        required_evidence = ["替代政策或自主可控表述", "被替代产品/环节", "国内产品成熟度", "公司客户或产能证据"]
    elif has_foreign_signal:
        template_id = "foreign_supply_chain_signal"
        mechanism = "海外供应、出口、提价或客户动作先影响供需预期，国内映射必须经过产品相似性、认证关系和产能承接证据确认。"
        required_evidence = ["海外供应链动作", "受影响产品/环节", "国内替代或二供可能性", "后续订单/认证验证"]
    else:
        return None
    return {
        "schema_version": "news_supply_chain_template.v1",
        "template_id": template_id,
        "mechanism": mechanism,
        "required_evidence": required_evidence,
        "confidence_rule": "industry_level_until_company_product_customer_evidence_is_explicit",
    }


def _supply_chain_template_steps(
    template: Dict[str, Any],
    company_impacts: List[Dict[str, Any]],
    related_boards: List[str],
    theme: str,
    mapping_score: float,
) -> List[Dict[str, Any]]:
    landing = _mapping_step_text(company_impacts, related_boards, theme)
    return [
        {
            "label": "海外供给/出口线索",
            "text": "识别海外限制、提价、扩产、出口或客户变化；先作为产业链扰动，不直接推断单家公司受益。",
            "score": 23.0,
        },
        {
            "label": "国产替代验证",
            "text": "必须继续核验国内可替代产品、客户认证、二供资格、材料设备或模组封测承接能力。",
            "score": 21.0,
        },
        {
            "label": "映射落点",
            "text": landing,
            "score": round(mapping_score, 1),
        },
    ]


def _event_category_for_text(text: str) -> str:
    haystack = str(text or "")
    if _supply_chain_template_for_text(haystack):
        return "供应链/替代"
    if any(term in haystack for term in ("提价", "涨价", "价格上涨", "售价", "均价", "合约价格", "合同价格", "ASP", "报价")):
        return "价格/供需"
    if any(term in haystack for term in ("量产", "投产", "交付", "出货", "供货", "订单", "定点", "认证", "客户导入")):
        return "大客户/订单"
    if any(term in haystack for term in ("突破", "路线", "工艺", "技术", "专利", "良率", "验证")):
        return "技术突破"
    if any(term in haystack for term in ("制裁", "禁令", "封锁", "限制", "关税", "出口管制", "供应受限", "断供", "国产替代", "进口替代", "自主可控", "二供", "出口")):
        return "供应链/替代"
    if any(term in haystack for term in ("政策", "规划", "征求意见", "补贴", "审评审批", "降准", "降息", "逆回购", "MLF", "LPR")):
        return "政策/宏观"
    if any(term in haystack for term in ("净利润", "业绩", "预告", "同比", "环比", "盈利")):
        return "业绩验证"
    if any(term in haystack for term in ("扩产", "产能", "建厂", "项目", "投建")):
        return "产能变化"
    return "消息催化"


def _catalyst_score(category: str, text: str) -> float:
    score = {
        "价格/供需": 24.0,
        "大客户/订单": 25.0,
        "技术突破": 22.0,
        "供应链/替代": 23.0,
        "政策/宏观": 20.0,
        "业绩验证": 21.0,
        "产能变化": 19.0,
    }.get(category, 15.0)
    if re.search(r"\d+(\.\d+)?\s*%|\d+(\.\d+)?\s*(亿|万|元|美元|韩元|日元|套|台)", str(text or "")):
        score += 3.0
    return min(score, 28.0)


def _transmission_score(theme: str, related_boards: List[str], company_impacts: List[Dict[str, Any]]) -> float:
    score = 10.0
    if theme:
        score += 4.0
    if related_boards:
        score += min(6.0, len(related_boards) * 1.5)
    if company_impacts:
        score += min(8.0, len(company_impacts) * 2.0)
    return min(score, 24.0)


def _mapping_score(company_impacts: List[Dict[str, Any]]) -> float:
    if not company_impacts:
        return 6.0
    confidences = [
        float(value)
        for value in (_float_or_none(item.get("confidence")) for item in company_impacts if isinstance(item, dict))
        if value is not None
    ]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.55
    explicit_bonus = 4.0 if any(str(item.get("role") or "").lower().startswith("cls explicit") for item in company_impacts if isinstance(item, dict)) else 0.0
    return round(min(24.0, 8.0 + len(company_impacts[:6]) * 1.6 + avg_confidence * 8.0 + explicit_bonus), 1)


def _chain_mechanism(category: str, theme: str, related_boards: List[str], company_impacts: List[Dict[str, Any]], rationale: str) -> str:
    boards = "、".join(related_boards[:3]) or theme
    company_text = "、".join(item.get("name") or item.get("symbol") or "" for item in company_impacts[:3] if isinstance(item, dict)) or "产业链标的"
    if category == "价格/供需":
        return f"{theme}价格或供需变化验证行业景气度，沿{boards}传导到具备库存、产能或产品结构弹性的{company_text}。"
    if category == "大客户/订单":
        return f"大客户量产、认证或订单变化先验证需求，再传导到已定点/已供货/可替代的{company_text}。"
    if category == "技术突破":
        return f"技术路线变化提高产业确定性，先影响{boards}，再映射到已有产品或工艺储备的{company_text}。"
    if category == "供应链/替代":
        return f"海外限制或供应链重构带来国产替代窗口，沿{boards}寻找国内可承接份额的{company_text}。"
    if category == "政策/宏观":
        return f"政策或流动性变化先影响风险偏好和产业约束，再传导到{boards}相关资产。"
    if category == "业绩验证":
        return f"业绩预告验证景气兑现，重点观察{theme}链条中收入、利润和订单弹性更高的{company_text}。"
    if category == "产能变化":
        return f"产能投放改变供需格局，需跟踪{boards}中受益于扩产配套或竞争格局变化的{company_text}。"
    return f"消息先影响{theme}预期，再通过{boards}映射到{company_text}。"


def _mapping_step_text(company_impacts: List[Dict[str, Any]], related_boards: List[str], theme: str) -> str:
    if company_impacts:
        names = [
            f"{item.get('name') or item.get('symbol')}({int(float(item.get('confidence') or 0) * 100)}%)"
            for item in company_impacts[:4]
            if isinstance(item, dict) and (item.get("name") or item.get("symbol"))
        ]
        if names:
            return f"公司映射：{'、'.join(names)}；低置信度映射只作为线索。"
    boards = "、".join(related_boards[:4]) or theme
    return f"暂无高置信公司映射，保留为产业级线索：{boards}。"


def _evidence_snippets(text: str) -> List[str]:
    cleaned = _clean_news_text(text)
    sentences = re.split(r"[。；;.!！?\n]", cleaned)
    snippets = [_compact_text(sentence, 90) for sentence in sentences if sentence.strip()]
    return snippets[:3]


def _chain_conclusion(category: str, theme: str, targets: List[str], score: float) -> str:
    target_text = "、".join(targets[:4]) or theme
    if score >= 80:
        strength = "强"
    elif score >= 65:
        strength = "中强"
    elif score >= 50:
        strength = "中等"
    else:
        strength = "偏弱"
    return f"{category}对{theme}形成{strength}催化，当前主要跟踪{target_text}的盘面验证和后续消息确认。"


def _score_from_parts(*, source: str, tone: str, theme_score: Any, company_count: int, evidence_grade: str, mapping_confidence: float) -> float:
    base = 42.0
    if source == "news_theme_daily":
        base += 8.0
    if source == "cls_telegraph":
        base += 5.0
    if tone == "positive":
        base += 10.0
    elif tone == "mixed":
        base += 4.0
    elif tone == "negative":
        base -= 8.0
    if evidence_grade == "confirmed":
        base += 8.0
    elif evidence_grade == "speculative":
        base -= 10.0
    base += min(8.0, company_count * 1.5)
    base += max(0.0, min(10.0, float(mapping_confidence or 0.0) * 10.0))
    try:
        base += max(-15.0, min(15.0, float(theme_score or 0.0) / 2.0))
    except Exception:
        pass
    return round(max(0.0, min(100.0, base)), 2)


def _summarize_cards(cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "total": len(cards),
        "active": sum(1 for item in cards if item.get("status") == "active"),
        "suppressed": sum(1 for item in cards if item.get("status") == "suppressed"),
        "mapping_counts": _count_dict(item.get("mapping_status") for item in cards),
        "horizon_counts": _count_dict(item.get("impact_horizon") for item in cards),
        "layer_counts": _count_dict(item.get("signal_layer") for item in cards),
        "top_industries": _top_values(industry for item in cards for industry in (item.get("primary_industries") or [])),
    }


def _count_dict(values: Iterable[Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _avg_float(values: Iterable[Any]) -> Optional[float]:
    numbers = [float(value) for value in (_float_or_none(item) for item in values) if value is not None]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 2)


def _top_values(values: Iterable[Any], *, limit: int = 8) -> List[Dict[str, Any]]:
    counts = _count_dict(values)
    return [{"key": key, "count": count} for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def _target_label(target_id: str) -> str:
    text = str(target_id or "")
    if ":" in text:
        return text.split(":", 1)[1]
    return text


def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _collect_graph_edges(
    repo: NewsSignalRepository,
    *,
    card_ids: List[str],
    signal_date: str,
    limit: int,
) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    if signal_date:
        for edge in repo.list_edges(signal_date=signal_date, limit=limit):
            by_id[str(edge.get("edge_id") or "")] = edge
    for card_id in card_ids:
        key = str(card_id or "").strip()
        if not key:
            continue
        for edge in repo.list_edges(card_id=key, limit=limit):
            by_id[str(edge.get("edge_id") or "")] = edge
    return [edge for _, edge in sorted(by_id.items()) if edge.get("edge_id")]


def _build_typed_relation_edges(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    edges: List[Dict[str, Any]] = []
    for card in cards:
        card_id = str(card.get("card_id") or "")
        if not card_id:
            continue
        for industry in (
            value
            for value in _unique_strings(card.get("primary_industries") or [])
            if value not in GENERIC_RELATION_THEMES
        ):
            target_type = "macro_theme" if card.get("signal_layer") == "macro" else "industry"
            edge_type = "affects_macro_theme" if target_type == "macro_theme" else "impacts_industry"
            edges.append(_edge_payload(
                source_card_id=card_id,
                target_type=target_type,
                target_id=f"{target_type}:{industry}",
                edge_class="typed_relation",
                edge_type=edge_type,
                weight=0.82,
                method="rule",
                rationale=f"新闻卡片主主题指向 {industry}。",
                evidence={
                    "industry": industry,
                    "signal_layer": card.get("signal_layer"),
                    "source_quality_score": _card_quality_score(card),
                    "signal_score": _card_signal_score(card),
                },
                decay_rule=str(card.get("decay_rule") or "3d"),
            ))
        for industry in (
            value
            for value in _unique_strings(card.get("secondary_industries") or [])
            if value not in GENERIC_RELATION_THEMES
        ):
            edges.append(_edge_payload(
                source_card_id=card_id,
                target_type="industry",
                target_id=f"industry:{industry}",
                edge_class="typed_relation",
                edge_type="related_industry",
                weight=0.56,
                method="rule",
                rationale=f"新闻卡片次级主题关联 {industry}。",
                evidence={
                    "industry": industry,
                    "signal_layer": card.get("signal_layer"),
                    "source_quality_score": _card_quality_score(card),
                    "signal_score": _card_signal_score(card),
                },
                decay_rule=str(card.get("decay_rule") or "3d"),
            ))
        for company in card.get("company_impacts") or []:
            if not isinstance(company, dict):
                continue
            symbol = str(company.get("symbol") or company.get("code") or "").strip()
            name = str(company.get("name") or "").strip()
            if not symbol and not name:
                continue
            confidence = _float_or_none(company.get("confidence"))
            weight = max(0.35, min(0.95, confidence if confidence is not None else 0.65))
            target_id = f"company:{symbol or name}"
            evidence = dict(company)
            evidence.update(
                {
                    "source_quality_score": _card_quality_score(card),
                    "signal_score": _card_signal_score(card),
                    "card_mapping_confidence": _float_or_none(card.get("mapping_confidence")) or 0.0,
                }
            )
            edges.append(_edge_payload(
                source_card_id=card_id,
                target_type="company",
                target_id=target_id,
                edge_class="typed_relation",
                edge_type="impacts_company",
                weight=weight,
                method="rule",
                rationale=f"新闻卡片映射到公司 {name or symbol}，方向 {company.get('direction') or 'unknown'}。",
                evidence=evidence,
                decay_rule=str(card.get("decay_rule") or "3d"),
            ))
        company_names = {str(item.get("name") or "").strip() for item in card.get("company_impacts") or [] if isinstance(item, dict)}
        for entity in _unique_strings(card.get("explicit_entities") or []):
            if entity in company_names:
                continue
            edges.append(_edge_payload(
                source_card_id=card_id,
                target_type="entity",
                target_id=f"entity:{entity}",
                edge_class="typed_relation",
                edge_type="mentions_entity",
                weight=0.5,
                method="rule",
                rationale=f"新闻原文显式提到实体 {entity}。",
                evidence={
                    "entity": entity,
                    "signal_layer": card.get("signal_layer"),
                    "source_quality_score": _card_quality_score(card),
                    "signal_score": _card_signal_score(card),
                },
                decay_rule=str(card.get("decay_rule") or "3d"),
            ))
    return _dedupe_edges(edges)


def _build_event_clue_edges(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    edges: List[Dict[str, Any]] = []
    for idx, left in enumerate(cards):
        for right in cards[idx + 1:]:
            left_id = str(left.get("card_id") or "")
            right_id = str(right.get("card_id") or "")
            if not left_id or not right_id or left_id == right_id:
                continue
            reasons: List[str] = []
            weight = 0.0
            left_companies = _company_keys(left)
            right_companies = _company_keys(right)
            common_companies = sorted(left_companies & right_companies)
            if common_companies:
                weight = max(weight, 0.88)
                reasons.append(f"共同公司：{'、'.join(common_companies[:5])}")
            common_primary = sorted(
                _meaningful_relation_themes(left.get("primary_industries"))
                & _meaningful_relation_themes(right.get("primary_industries"))
            )
            if common_primary:
                weight = max(weight, 0.74)
                reasons.append(f"共同主主题：{'、'.join(common_primary[:5])}")
            common_secondary = sorted(
                _meaningful_relation_themes(left.get("secondary_industries"))
                & _meaningful_relation_themes(right.get("secondary_industries"))
            )
            if common_secondary:
                weight = max(weight, 0.62)
                reasons.append(f"共同次级主题：{'、'.join(common_secondary[:5])}")
            if left.get("signal_layer") == right.get("signal_layer") == "macro" and common_primary:
                weight = max(weight, 0.8)
                reasons.append("同类宏观变量线索")
            if not reasons:
                continue
            common_event_categories = sorted(_event_categories(left) & _event_categories(right))
            common_entities = sorted(
                set(_unique_strings(left.get("explicit_entities") or []))
                & set(_unique_strings(right.get("explicit_entities") or []))
            )
            text_similarity = _character_shingle_similarity(
                str(left.get("summary_short") or ""),
                str(right.get("summary_short") or ""),
            )
            time_distance_days = _time_distance_days(left, right)
            same_event = bool(
                common_event_categories
                and (common_companies or common_entities)
                and text_similarity >= 0.22
                and (time_distance_days is None or time_distance_days <= 3)
            )
            if same_event:
                weight = max(weight, 0.92)
                reasons.insert(
                    0,
                    f"同一事件演化候选：事件类型 {'、'.join(common_event_categories[:3])}，文本相似度 {text_similarity:.2f}",
                )
            source, target = _ordered_card_pair(left, right)
            edge_type = "same_event" if same_event else ("same_company" if common_companies else ("same_macro_theme" if left.get("signal_layer") == right.get("signal_layer") == "macro" else "same_theme"))
            edges.append(_edge_payload(
                source_card_id=source,
                target_type="card",
                target_id=target,
                target_card_id=target,
                edge_class="event_clue",
                edge_type=edge_type,
                weight=weight,
                method="rule",
                rationale="；".join(reasons),
                evidence={
                    "common_companies": common_companies,
                    "common_primary_industries": common_primary,
                    "common_secondary_industries": common_secondary,
                    "common_event_categories": common_event_categories,
                    "common_entities": common_entities,
                    "text_similarity": round(text_similarity, 6),
                    "same_event": same_event,
                    **_edge_pair_evidence(left, right),
                },
                threshold_profile="news-card-event-clue.v2",
                decay_rule="14d",
            ))
    return _dedupe_edges(edges)


def _event_categories(card: Dict[str, Any]) -> set[str]:
    return {
        str(path.get("event_category") or "").strip()
        for path in card.get("transmission_paths") or []
        if isinstance(path, dict) and str(path.get("event_category") or "").strip()
    }


def _meaningful_relation_themes(values: Any) -> set[str]:
    return {
        value
        for value in _unique_strings(values or [])
        if value not in GENERIC_RELATION_THEMES
    }


def _character_shingle_similarity(left: str, right: str) -> float:
    def _shingles(value: str) -> set[str]:
        normalized = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())
        if len(normalized) < 2:
            return {normalized} if normalized else set()
        return {normalized[index:index + 2] for index in range(len(normalized) - 1)}

    left_values = _shingles(left)
    right_values = _shingles(right)
    if not left_values or not right_values:
        return 0.0
    return len(left_values & right_values) / len(left_values | right_values)


def _card_quality_score(card: Dict[str, Any]) -> float:
    diagnostics = card.get("diagnostics") if isinstance(card.get("diagnostics"), dict) else {}
    raw_quality = diagnostics.get("raw_quality") if isinstance(diagnostics.get("raw_quality"), dict) else {}
    score = _float_or_none(raw_quality.get("score"))
    if score is not None:
        return max(0.0, min(100.0, score))
    if card.get("status") == "low_quality":
        return 25.0
    return 70.0


def _card_signal_score(card: Dict[str, Any]) -> float:
    score = _float_or_none(card.get("signal_score"))
    return max(0.0, min(100.0, score if score is not None else 50.0))


def _time_distance_days(left: Dict[str, Any], right: Dict[str, Any]) -> Optional[int]:
    left_date = _parse_date(left.get("signal_date"))
    right_date = _parse_date(right.get("signal_date"))
    if not left_date or not right_date:
        return None
    return abs((left_date - right_date).days)


def _edge_pair_evidence(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    left_companies = _company_keys(left)
    right_companies = _company_keys(right)
    common_companies = sorted(left_companies & right_companies)
    common_primary = sorted(
        set(_unique_strings(left.get("primary_industries") or []))
        & set(_unique_strings(right.get("primary_industries") or []))
    )
    common_secondary = sorted(
        set(_unique_strings(left.get("secondary_industries") or []))
        & set(_unique_strings(right.get("secondary_industries") or []))
    )
    left_layer = str(left.get("signal_layer") or "")
    right_layer = str(right.get("signal_layer") or "")
    left_quality = _card_quality_score(left)
    right_quality = _card_quality_score(right)
    return {
        "common_companies": common_companies,
        "common_primary_industries": common_primary,
        "common_secondary_industries": common_secondary,
        "source_layers": [left_layer, right_layer],
        "same_signal_layer": bool(left_layer and left_layer == right_layer),
        "time_distance_days": _time_distance_days(left, right),
        "min_source_quality_score": round(min(left_quality, right_quality), 2),
        "min_signal_score": round(min(_card_signal_score(left), _card_signal_score(right)), 2),
    }


def _edge_quality(
    *,
    edge_class: str,
    edge_type: str,
    target_type: str,
    weight: float,
    evidence: Dict[str, Any],
) -> Dict[str, Any]:
    flags: List[str] = []
    normalized_weight = max(0.0, min(1.0, float(weight or 0.0)))
    if edge_class == "typed_relation":
        score = 50.0 + normalized_weight * 45.0
        if edge_type in {"impacts_company", "impacts_industry", "affects_macro_theme"}:
            score += 5.0
        if edge_type == "related_industry":
            score -= 12.0
            flags.append("secondary_theme_relation")
        if edge_type == "mentions_entity":
            score -= 20.0
            flags.append("entity_mention_only")
        confidence = _float_or_none(evidence.get("confidence"))
        if confidence is None:
            confidence = _float_or_none(evidence.get("card_mapping_confidence"))
        if edge_type == "impacts_company" and confidence is not None and confidence < 0.65:
            score -= 12.0
            flags.append("low_mapping_confidence")
        mapping_status = str(evidence.get("mapping_status") or "")
        if edge_type == "impacts_company" and mapping_status and mapping_status != "mapped":
            score -= 8.0
            flags.append(f"mapping_status_{mapping_status}")
    elif edge_class == "event_clue":
        score = 40.0 + normalized_weight * 45.0
        if evidence.get("common_companies"):
            score += 10.0
        elif evidence.get("common_primary_industries"):
            score += 5.0
        elif evidence.get("common_secondary_industries"):
            score -= 12.0
            flags.append("theme_cluster_only")
        if edge_type == "same_macro_theme":
            score += 6.0
    elif edge_class == "semantic_similarity":
        score = 10.0 + normalized_weight * 55.0
        flags.append("semantic_not_causal")
        if evidence.get("common_companies"):
            score += 20.0
        if evidence.get("common_primary_industries"):
            score += 12.0
        elif evidence.get("common_secondary_industries"):
            score += 6.0
        if evidence.get("same_signal_layer"):
            score += 5.0
        else:
            score -= 12.0
            flags.append("cross_layer_semantic")
        if not evidence.get("common_companies") and not evidence.get("common_primary_industries") and not evidence.get("common_secondary_industries"):
            score -= 10.0
            flags.append("weak_semantic_only")
        similarity = _float_or_none(evidence.get("similarity"))
        threshold = _float_or_none(evidence.get("threshold"))
        if similarity is not None and threshold is not None and similarity - threshold < 0.03:
            score -= 8.0
            flags.append("near_embedding_threshold")
    else:
        score = 35.0 + normalized_weight * 40.0
        flags.append("unknown_edge_class")

    source_quality = _float_or_none(evidence.get("min_source_quality_score"))
    if source_quality is None:
        source_quality = _float_or_none(evidence.get("source_quality_score"))
    if source_quality is not None:
        if source_quality < 45:
            score -= 25.0
            flags.append("low_source_quality")
        elif source_quality < 60:
            score -= 8.0
            flags.append("medium_source_quality")

    min_signal = _float_or_none(evidence.get("min_signal_score"))
    if min_signal is None:
        min_signal = _float_or_none(evidence.get("signal_score"))
    if min_signal is not None and min_signal < 45:
        score -= 6.0
        flags.append("low_signal_score")

    distance = _float_or_none(evidence.get("time_distance_days"))
    if distance is not None:
        if distance > 14:
            score -= 18.0
            flags.append("stale_relation")
        elif distance > 5:
            score -= 8.0
            flags.append("distant_relation")

    score = round(max(0.0, min(100.0, score)), 2)
    if score >= 75:
        grade = "high"
    elif score >= 55:
        grade = "medium"
    else:
        grade = "low"
    return {"score": score, "grade": grade, "flags": _unique_strings(flags)}


def _limit_semantic_edges(edges: List[Dict[str, Any]], *, per_card_limit: int) -> List[Dict[str, Any]]:
    if per_card_limit <= 0:
        return []
    counts: Dict[str, int] = {}
    selected: List[Dict[str, Any]] = []
    sorted_edges = sorted(
        edges,
        key=lambda item: (
            -float(item.get("edge_quality") or 0.0),
            -float(item.get("weight") or 0.0),
            str(item.get("edge_id") or ""),
        ),
    )
    for edge in sorted_edges:
        source = str(edge.get("source_card_id") or "")
        target = str(edge.get("target_card_id") or edge.get("target_id") or "")
        if not source or not target:
            continue
        if counts.get(source, 0) >= per_card_limit or counts.get(target, 0) >= per_card_limit:
            continue
        selected.append(edge)
        counts[source] = counts.get(source, 0) + 1
        counts[target] = counts.get(target, 0) + 1
    return selected


def _edge_payload(
    *,
    source_card_id: str,
    target_type: str,
    target_id: str,
    edge_class: str,
    edge_type: str,
    weight: float,
    method: str,
    rationale: str,
    evidence: Optional[Dict[str, Any]] = None,
    target_card_id: str = "",
    embedding_model: str = "",
    threshold_profile: str = "",
    decay_rule: str = "none",
) -> Dict[str, Any]:
    normalized_weight = round(max(0.0, min(1.0, float(weight or 0.0))), 6)
    evidence_payload = evidence or {}
    quality = _edge_quality(
        edge_class=edge_class,
        edge_type=edge_type,
        target_type=target_type,
        weight=normalized_weight,
        evidence=evidence_payload,
    )
    return {
        "edge_id": _stable_id("edge", source_card_id, target_type, target_id, edge_type),
        "source_card_id": source_card_id,
        "target_card_id": target_card_id,
        "target_type": target_type,
        "target_id": target_id,
        "edge_class": edge_class,
        "edge_type": edge_type,
        "weight": normalized_weight,
        "edge_quality": quality["score"],
        "quality_grade": quality["grade"],
        "quality_flags": quality["flags"],
        "method": method,
        "rationale": rationale,
        "evidence": evidence_payload,
        "embedding_model": embedding_model or "",
        "threshold_profile": threshold_profile or "",
        "decay_rule": decay_rule,
        "status": "active",
    }


def _dedupe_edges(edges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_identity: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for edge in edges:
        identity = (
            str(edge.get("source_card_id") or ""),
            str(edge.get("target_type") or ""),
            str(edge.get("target_id") or ""),
            str(edge.get("edge_type") or ""),
        )
        existing = by_identity.get(identity)
        if existing is None or (
            float(edge.get("edge_quality") or 0.0),
            float(edge.get("weight") or 0.0),
        ) > (
            float(existing.get("edge_quality") or 0.0),
            float(existing.get("weight") or 0.0),
        ):
            by_identity[identity] = edge
    return list(by_identity.values())


def _company_keys(card: Dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for item in card.get("company_impacts") or []:
        if not isinstance(item, dict):
            continue
        for value in (item.get("symbol"), item.get("code"), item.get("ts_code"), item.get("name")):
            text = str(value or "").strip()
            if text:
                keys.add(text)
    return keys


def _ordered_card_pair(left: Dict[str, Any], right: Dict[str, Any]) -> Tuple[str, str]:
    left_key = (
        str(left.get("valid_from") or left.get("signal_date") or ""),
        str(left.get("card_id") or ""),
    )
    right_key = (
        str(right.get("valid_from") or right.get("signal_date") or ""),
        str(right.get("card_id") or ""),
    )
    if left_key <= right_key:
        return str(left.get("card_id") or ""), str(right.get("card_id") or "")
    return str(right.get("card_id") or ""), str(left.get("card_id") or "")


def _news_edge_text(card: Dict[str, Any]) -> str:
    companies = [
        str(item.get("name") or item.get("symbol") or "")
        for item in card.get("company_impacts") or []
        if isinstance(item, dict)
    ]
    return " ".join(
        part
        for part in [
            str(card.get("summary_short") or ""),
            " ".join(_unique_strings(card.get("primary_industries") or [])),
            " ".join(_unique_strings(card.get("secondary_industries") or [])),
            " ".join(_unique_strings(card.get("explicit_entities") or [])),
            " ".join(_unique_strings(companies)),
        ]
        if part
    )


def _cosine_similarity(left: List[float], right: List[float]) -> float:
    if not left or not right:
        return 0.0
    size = min(len(left), len(right))
    dot = sum(float(left[i]) * float(right[i]) for i in range(size))
    left_norm = math.sqrt(sum(float(left[i]) ** 2 for i in range(size)))
    right_norm = math.sqrt(sum(float(right[i]) ** 2 for i in range(size)))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return round(dot / (left_norm * right_norm), 6)


def _similarity_distribution(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {"sample_count": 0, "min": None, "p50": None, "p90": None, "max": None}
    ordered = sorted(float(value) for value in values)

    def _percentile(fraction: float) -> float:
        index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
        return round(ordered[index], 6)

    return {
        "sample_count": len(ordered),
        "min": round(ordered[0], 6),
        "p50": _percentile(0.5),
        "p90": _percentile(0.9),
        "max": round(ordered[-1], 6),
    }


def _run_async(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def _select_company_for_stock(companies: List[Dict[str, Any]], symbol: str) -> Dict[str, Any]:
    if not companies:
        return {}
    normalized = str(symbol or "").strip().upper()
    if normalized:
        for item in companies:
            if str(item.get("symbol") or "").strip().upper() == normalized:
                return item
    return companies[0]


def _evidence_direction(value: Any) -> str:
    text = str(value or "").lower()
    if text in {"benefit", "positive"}:
        return "positive"
    if text in {"harm", "negative"}:
        return "negative"
    if text in {"mixed", "uncertain"}:
        return "mixed"
    if text == "neutral":
        return "neutral"
    return "unknown"


def _strength_from_value(value: Any) -> str:
    text = str(value or "").lower()
    if text in {"strong", "extreme"}:
        return text
    if text in {"medium", "weak"}:
        return text
    return "medium"


def _strength_from_confidence(value: Any) -> str:
    try:
        confidence = float(value or 0.0)
    except Exception:
        confidence = 0.0
    if confidence >= 0.85:
        return "strong"
    if confidence >= 0.6:
        return "medium"
    return "weak"


def _score_delta_from_direction(direction: Any, score: Any) -> float:
    magnitude = max(1.0, min(12.0, float(score or 0.0) / 10.0))
    text = str(direction or "").lower()
    if text in {"harm", "negative"}:
        return -magnitude
    if text in {"benefit", "positive"}:
        return magnitude
    return 0.0


def _confidence_from_card(card: Dict[str, Any]) -> float:
    grade = str(card.get("evidence_grade") or "")
    mapping = str(card.get("mapping_status") or "")
    base = 0.45
    if grade == "confirmed":
        base = 0.78
    elif grade == "plausible":
        base = 0.62
    elif grade == "speculative":
        base = 0.35
    if mapping == "mapped":
        base += 0.08
    elif mapping == "ambiguous":
        base -= 0.12
    elif mapping == "unmapped":
        base -= 0.2
    return max(0.0, min(1.0, round(base, 4)))


def _stance_from_impact(value: Any) -> str:
    text = str(value or "").lower()
    if text == "positive":
        return "support"
    if text == "negative":
        return "oppose"
    if text == "mixed":
        return "wait_confirm"
    return "neutral"
