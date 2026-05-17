#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke-test API credentials configured through `.env`.

The tool only tests API families that are actually configured. Notification
webhooks are dry-run by default to avoid sending messages; pass
`--notify-send` when you explicitly want to send a test notification.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import requests


DEFAULT_CATEGORIES = ("llm", "search", "data", "graph", "sentiment", "notify")
_SENSITIVE_QUERY_RE = r"(?i)(api_key|apikey|token|access_token|key|secret)=([^&\s]+)"


def configure_proxy_from_env() -> None:
    """Honor the historical USE_PROXY switch used by this helper."""
    if os.getenv("GITHUB_ACTIONS") == "true":
        return
    if os.getenv("USE_PROXY", "false").lower() != "true":
        return
    proxy_host = os.getenv("PROXY_HOST", "127.0.0.1")
    proxy_port = os.getenv("PROXY_PORT", "10809")
    proxy_url = f"http://{proxy_host}:{proxy_port}"
    os.environ["http_proxy"] = proxy_url
    os.environ["https_proxy"] = proxy_url


@dataclass
class ApiCheckResult:
    category: str
    name: str
    configured: bool
    success: bool = False
    skipped: bool = False
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if not self.configured:
            return "UNSET"
        if self.skipped:
            return "SKIP"
        return "OK" if self.success else "FAIL"

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["message"] = redact_sensitive_text(payload.get("message", ""))
        payload["status"] = self.status
        return payload


def redact_sensitive_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""

    import re

    def _replace(match: Any) -> str:
        return f"{match.group(1)}={mask_secret(match.group(2))}"

    text = re.sub(_SENSITIVE_QUERY_RE, _replace, text)
    return text


def split_csv(value: Optional[str]) -> List[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def mask_secret(value: Optional[str]) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}...{text[-4:]}"


def clean_error(exc: BaseException, *, limit: int = 400) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return redact_sensitive_text(message[:limit])


def select_categories(args: argparse.Namespace) -> Set[str]:
    selected = {
        name
        for name in ("llm", "search", "data", "graph", "sentiment", "notify")
        if getattr(args, name, False)
    }
    if getattr(args, "fetch", False):
        selected.add("data")
    if args.all or not selected:
        return set(DEFAULT_CATEGORIES)
    return selected


def selected_keys(keys: Sequence[str], *, all_keys: bool) -> List[str]:
    cleaned = [key for key in keys if key]
    if all_keys:
        return cleaned
    return cleaned[:1]


def load_runtime_config():
    from src.config import Config, get_config, setup_env

    Config.reset_instance()
    setup_env()
    return get_config()


def result_from_exception(category: str, name: str, exc: BaseException) -> ApiCheckResult:
    return ApiCheckResult(
        category=category,
        name=name,
        configured=True,
        success=False,
        message=clean_error(exc),
    )


def _llm_completion_success(response: Any) -> bool:
    choices = getattr(response, "choices", None)
    if not choices and isinstance(response, dict):
        choices = response.get("choices")
    return bool(choices)


def run_llm_checks(config: Any, *, timeout: float, all_keys: bool) -> List[ApiCheckResult]:
    results: List[ApiCheckResult] = []

    channels = getattr(config, "llm_channels", []) or []
    if channels:
        from src.services.system_config_service import SystemConfigService

        service = SystemConfigService()
        for channel in channels:
            if channel.get("enabled") is False:
                results.append(
                    ApiCheckResult(
                        category="llm",
                        name=f"LLM channel:{channel.get('name') or 'channel'}",
                        configured=True,
                        skipped=True,
                        message="channel disabled",
                    )
                )
                continue
            raw_keys = split_csv(channel.get("api_key") or ",".join(channel.get("api_keys") or []))
            key_variants = selected_keys(raw_keys, all_keys=all_keys) or [""]
            for index, api_key in enumerate(key_variants, start=1):
                suffix = f" key {index}/{len(raw_keys)}" if raw_keys and all_keys else ""
                name = f"LLM channel:{channel.get('name') or 'channel'}{suffix}"
                try:
                    response = service.test_llm_channel(
                        name=str(channel.get("name") or "channel"),
                        protocol=str(channel.get("protocol") or ""),
                        base_url=str(channel.get("base_url") or ""),
                        api_key=api_key,
                        models=channel.get("models") or [],
                        enabled=channel.get("enabled", True),
                        timeout_seconds=timeout,
                    )
                    results.append(
                        ApiCheckResult(
                            category="llm",
                            name=name,
                            configured=True,
                            success=bool(response.get("success")),
                            message=response.get("message") or response.get("error") or "",
                            details={
                                "model": response.get("resolved_model"),
                                "protocol": response.get("resolved_protocol"),
                                "latency_ms": response.get("latency_ms"),
                                "key": mask_secret(api_key),
                            },
                        )
                    )
                except Exception as exc:
                    results.append(result_from_exception("llm", name, exc))
        return results

    model = (getattr(config, "litellm_model", "") or "").strip()
    model_list = getattr(config, "llm_model_list", []) or []
    if not model and not model_list:
        return [
            ApiCheckResult(
                category="llm",
                name="LLM",
                configured=False,
                skipped=True,
                message="no LITELLM_MODEL, LLM_CHANNELS, LITELLM_CONFIG, or legacy provider API key configured",
            )
        ]

    try:
        import litellm

        from src.agent.llm_adapter import LLMToolAdapter
        from src.config import extra_litellm_params, get_api_keys_for_model

        LLMToolAdapter._register_custom_model_pricing()
        messages = [{"role": "user", "content": "Reply with OK"}]

        if model_list and getattr(config, "llm_models_source", "") == "litellm_config":
            target_model = model or str(model_list[0].get("model_name") or "")
            router = litellm.Router(model_list=model_list)
            started = time.perf_counter()
            response = router.completion(
                model=target_model,
                messages=messages,
                temperature=getattr(config, "llm_temperature", 0.7),
                max_tokens=16,
                timeout=timeout,
            )
            results.append(
                ApiCheckResult(
                    category="llm",
                    name="LLM config",
                    configured=True,
                    success=_llm_completion_success(response),
                    message="LiteLLM config completion succeeded",
                    details={"model": target_model, "latency_ms": int((time.perf_counter() - started) * 1000)},
                )
            )
            return results

        api_keys = get_api_keys_for_model(model, config)
        key_variants = selected_keys(api_keys, all_keys=all_keys) or [""]
        for index, api_key in enumerate(key_variants, start=1):
            name = f"LLM:{model}" + (f" key {index}/{len(api_keys)}" if api_keys and all_keys else "")
            kwargs: Dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": getattr(config, "llm_temperature", 0.7),
                "max_tokens": 16,
                "timeout": timeout,
            }
            if api_key:
                kwargs["api_key"] = api_key
            kwargs.update(extra_litellm_params(model, config))
            started = time.perf_counter()
            response = litellm.completion(**kwargs)
            results.append(
                ApiCheckResult(
                    category="llm",
                    name=name,
                    configured=True,
                    success=_llm_completion_success(response),
                    message="LiteLLM completion succeeded",
                    details={"model": model, "latency_ms": int((time.perf_counter() - started) * 1000), "key": mask_secret(api_key)},
                )
            )
    except Exception as exc:
        results.append(result_from_exception("llm", f"LLM:{model or 'configured'}", exc))

    return results


def _run_search_provider(
    *,
    name: str,
    provider_factory: Any,
    keys: Sequence[str],
    all_keys: bool,
) -> List[ApiCheckResult]:
    results: List[ApiCheckResult] = []
    if not keys:
        return results
    for index, key in enumerate(selected_keys(keys, all_keys=all_keys), start=1):
        label = f"{name}" + (f" key {index}/{len(keys)}" if all_keys and len(keys) > 1 else "")
        try:
            provider = provider_factory([key])
            response = provider.search("stock market news", max_results=1, days=7)
            results.append(
                ApiCheckResult(
                    category="search",
                    name=label,
                    configured=True,
                    success=bool(response.success),
                    message="search request succeeded" if response.success else (response.error_message or "search request failed"),
                    details={"results": len(response.results), "key": mask_secret(key)},
                )
            )
        except Exception as exc:
            results.append(result_from_exception("search", label, exc))
    return results


def run_search_checks(config: Any, *, all_keys: bool) -> List[ApiCheckResult]:
    from src.search_service import (
        AnspireSearchProvider,
        BochaSearchProvider,
        BraveSearchProvider,
        MiniMaxSearchProvider,
        SearXNGSearchProvider,
        SerpAPISearchProvider,
        TavilySearchProvider,
    )

    results: List[ApiCheckResult] = []
    providers = [
        ("Tavily", TavilySearchProvider, getattr(config, "tavily_api_keys", []) or []),
        ("Bocha", BochaSearchProvider, getattr(config, "bocha_api_keys", []) or []),
        ("Brave", BraveSearchProvider, getattr(config, "brave_api_keys", []) or []),
        ("SerpAPI", SerpAPISearchProvider, getattr(config, "serpapi_keys", []) or []),
        ("Anspire", AnspireSearchProvider, getattr(config, "anspire_api_keys", []) or []),
        ("MiniMax", MiniMaxSearchProvider, getattr(config, "minimax_api_keys", []) or []),
    ]
    for name, factory, keys in providers:
        results.extend(_run_search_provider(name=name, provider_factory=factory, keys=keys, all_keys=all_keys))

    searxng_urls = getattr(config, "searxng_base_urls", []) or []
    if searxng_urls:
        try:
            provider = SearXNGSearchProvider(base_urls=searxng_urls)
            response = provider.search("stock market news", max_results=1, days=7)
            results.append(
                ApiCheckResult(
                    category="search",
                    name="SearXNG",
                    configured=True,
                    success=bool(response.success),
                    message="search request succeeded" if response.success else (response.error_message or "search request failed"),
                    details={"instances": len(searxng_urls), "results": len(response.results)},
                )
            )
        except Exception as exc:
            results.append(result_from_exception("search", "SearXNG", exc))

    if not results:
        results.append(
            ApiCheckResult(
                category="search",
                name="Search APIs",
                configured=False,
                skipped=True,
                message="no search API env vars configured",
            )
        )
    return results


def run_tushare_check(token: Optional[str], *, timeout: float) -> ApiCheckResult:
    if not token:
        return ApiCheckResult(category="data", name="Tushare", configured=False, skipped=True, message="TUSHARE_TOKEN not configured")

    def post(api_name: str, params: Dict[str, Any], fields: str) -> Dict[str, Any]:
        payload = {
            "api_name": api_name,
            "token": token,
            "params": params,
            "fields": fields,
        }
        response = requests.post("http://api.tushare.pro", json=payload, timeout=timeout)
        if response.status_code != 200:
            return {"ok": False, "message": f"HTTP {response.status_code}"}
        data = response.json()
        return {
            "ok": data.get("code") == 0,
            "message": "ok" if data.get("code") == 0 else str(data.get("msg") or data)[:300],
            "items": len((data.get("data") or {}).get("items") or []),
        }

    try:
        probes = {
            "daily": post(
                "daily",
                {"ts_code": "600519.SH", "start_date": "20260401", "end_date": "20260430"},
                "ts_code,trade_date,close",
            ),
            "trade_cal": post(
                "trade_cal",
                {
                    "exchange": "",
                    "start_date": "20180901",
                    "end_date": "20181001",
                    "is_open": "0",
                },
                "exchange,cal_date,is_open,pretrade_date",
            ),
        }
        ok = any(item["ok"] for item in probes.values())
        if ok:
            failed = [f"{name}: {item['message']}" for name, item in probes.items() if not item["ok"]]
            message = "Tushare request succeeded"
            if failed:
                message += f"; limited endpoint(s): {'; '.join(failed)}"
        else:
            message = "; ".join(f"{name}: {item['message']}" for name, item in probes.items())
        return ApiCheckResult(
            category="data",
            name="Tushare",
            configured=True,
            success=ok,
            message=message,
            details={"key": mask_secret(token), "probes": probes},
        )
    except Exception as exc:
        return result_from_exception("data", "Tushare", exc)


def run_tickflow_check(api_key: Optional[str], *, timeout: float) -> ApiCheckResult:
    if not api_key:
        return ApiCheckResult(category="data", name="TickFlow", configured=False, skipped=True, message="TICKFLOW_API_KEY not configured")
    try:
        from data_provider.tickflow_fetcher import TickFlowFetcher

        fetcher = TickFlowFetcher(api_key=api_key, timeout=timeout)
        try:
            indices = fetcher.get_main_indices("cn")
        finally:
            fetcher.close()
        ok = bool(indices)
        return ApiCheckResult(
            category="data",
            name="TickFlow",
            configured=True,
            success=ok,
            message="main index quote request succeeded" if ok else "main index quote request returned no data",
            details={"items": len(indices or []), "key": mask_secret(api_key)},
        )
    except Exception as exc:
        return result_from_exception("data", "TickFlow", exc)


def run_longbridge_check(config: Any) -> ApiCheckResult:
    if not (
        getattr(config, "longbridge_app_key", None)
        and getattr(config, "longbridge_app_secret", None)
        and getattr(config, "longbridge_access_token", None)
    ):
        return ApiCheckResult(category="data", name="Longbridge", configured=False, skipped=True, message="Longbridge credentials not configured")
    try:
        from data_provider.longbridge_fetcher import LongbridgeFetcher

        fetcher = LongbridgeFetcher()
        name = fetcher.get_stock_name("AAPL")
        ctx = getattr(fetcher, "_ctx", None)
        close = getattr(ctx, "close", None)
        if callable(close):
            close()
        return ApiCheckResult(
            category="data",
            name="Longbridge",
            configured=True,
            success=bool(name),
            message="static_info request succeeded" if name else "static_info request returned no data",
            details={"symbol": "AAPL.US", "name": name or ""},
        )
    except Exception as exc:
        return result_from_exception("data", "Longbridge", exc)


def run_data_checks(config: Any, *, timeout: float) -> List[ApiCheckResult]:
    return [
        run_tushare_check(getattr(config, "tushare_token", None), timeout=timeout),
        run_tickflow_check(getattr(config, "tickflow_api_key", None), timeout=timeout),
        run_longbridge_check(config),
    ]


def run_neo4j_check(config: Any) -> ApiCheckResult:
    uri = getattr(config, "graphiti_neo4j_uri", None)
    user = getattr(config, "graphiti_neo4j_user", None)
    password = getattr(config, "graphiti_neo4j_password", None)
    if not (uri and user and password):
        return ApiCheckResult(
            category="graph",
            name="Neo4j",
            configured=False,
            skipped=True,
            message="NEO4J_URI, NEO4J_USER, or NEO4J_PASSWORD not configured",
        )

    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(uri, auth=(user, password))
        try:
            started = time.perf_counter()
            driver.verify_connectivity()
            latency_ms = int((time.perf_counter() - started) * 1000)
        finally:
            driver.close()
        return ApiCheckResult(
            category="graph",
            name="Neo4j",
            configured=True,
            success=True,
            message="Neo4j connectivity check succeeded",
            details={"uri": uri, "user": user, "latency_ms": latency_ms},
        )
    except Exception as exc:
        return result_from_exception("graph", "Neo4j", exc)


def _embedding_success(response: Any) -> bool:
    data = getattr(response, "data", None)
    if data is None and isinstance(response, dict):
        data = response.get("data")
    if not data:
        return False
    first = data[0]
    if isinstance(first, dict):
        return bool(first.get("embedding"))
    return bool(getattr(first, "embedding", None))


def run_graphiti_embedding_check(config: Any, *, timeout: float) -> ApiCheckResult:
    model = (getattr(config, "graphiti_embedding_model", "") or "").strip()
    api_base = getattr(config, "graphiti_embedding_base_url", None)
    api_key = getattr(config, "graphiti_embedding_api_key", None)
    if not model:
        return ApiCheckResult(
            category="graph",
            name="Graphiti embedding",
            configured=False,
            skipped=True,
            message="GRAPHITI_EMBEDDING_MODEL not configured",
        )

    try:
        import litellm

        kwargs: Dict[str, Any] = {
            "model": model,
            "input": ["daily_stock_analysis graph smoke test"],
            "timeout": timeout,
        }
        if api_base:
            kwargs["api_base"] = api_base
        if api_key:
            kwargs["api_key"] = api_key
        started = time.perf_counter()
        response = litellm.embedding(**kwargs)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ApiCheckResult(
            category="graph",
            name=f"Graphiti embedding:{model}",
            configured=True,
            success=_embedding_success(response),
            message="embedding request succeeded" if _embedding_success(response) else "embedding response contained no vector",
            details={
                "model": model,
                "base_url": api_base or "",
                "key": mask_secret(api_key),
                "latency_ms": latency_ms,
            },
        )
    except Exception as exc:
        return result_from_exception("graph", f"Graphiti embedding:{model}", exc)


def run_graph_checks(config: Any, *, timeout: float) -> List[ApiCheckResult]:
    return [
        run_neo4j_check(config),
        run_graphiti_embedding_check(config, timeout=timeout),
    ]


def run_sentiment_checks(config: Any) -> List[ApiCheckResult]:
    api_key = getattr(config, "social_sentiment_api_key", None)
    if not api_key:
        return [
            ApiCheckResult(
                category="sentiment",
                name="Social Sentiment",
                configured=False,
                skipped=True,
                message="SOCIAL_SENTIMENT_API_KEY not configured",
            )
        ]
    try:
        from src.services.social_sentiment_service import SocialSentimentService

        svc = SocialSentimentService(api_key=api_key, api_url=getattr(config, "social_sentiment_api_url", "https://api.adanos.org"))
        data = svc.fetch_reddit_trending()
        return [
            ApiCheckResult(
                category="sentiment",
                name="Social Sentiment",
                configured=True,
                success=data is not None,
                message="reddit trending request succeeded" if data is not None else "reddit trending request returned no data",
                details={"items": len(data or []), "key": mask_secret(api_key)},
            )
        ]
    except Exception as exc:
        return [result_from_exception("sentiment", "Social Sentiment", exc)]


def _feishu_sign(secret: str, timestamp: str) -> str:
    key = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def run_notify_checks(config: Any, *, timeout: float, send: bool) -> List[ApiCheckResult]:
    results: List[ApiCheckResult] = []

    wechat_url = getattr(config, "wechat_webhook_url", None)
    if wechat_url:
        if not send:
            results.append(ApiCheckResult(category="notify", name="WeChat Work", configured=True, skipped=True, message="dry-run; pass --notify-send to send a test message"))
        else:
            try:
                response = requests.post(
                    wechat_url,
                    json={"msgtype": "text", "text": {"content": "daily_stock_analysis API smoke test"}},
                    timeout=timeout,
                )
                results.append(ApiCheckResult(category="notify", name="WeChat Work", configured=True, success=response.status_code == 200 and response.json().get("errcode") == 0, message=response.text[:300]))
            except Exception as exc:
                results.append(result_from_exception("notify", "WeChat Work", exc))

    feishu_url = getattr(config, "feishu_webhook_url", None)
    if feishu_url:
        if not send:
            results.append(ApiCheckResult(category="notify", name="Feishu", configured=True, skipped=True, message="dry-run; pass --notify-send to send a test message"))
        else:
            try:
                text = "daily_stock_analysis API smoke test"
                if getattr(config, "feishu_webhook_keyword", None):
                    text = f"{config.feishu_webhook_keyword} {text}"
                payload: Dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
                secret = getattr(config, "feishu_webhook_secret", None)
                if secret:
                    timestamp = str(int(time.time()))
                    payload["timestamp"] = timestamp
                    payload["sign"] = _feishu_sign(secret, timestamp)
                response = requests.post(feishu_url, json=payload, timeout=timeout)
                body = response.json()
                results.append(ApiCheckResult(category="notify", name="Feishu", configured=True, success=response.status_code == 200 and body.get("code") == 0, message=str(body)[:300]))
            except Exception as exc:
                results.append(result_from_exception("notify", "Feishu", exc))

    telegram_token = getattr(config, "telegram_bot_token", None)
    if telegram_token:
        try:
            if send and getattr(config, "telegram_chat_id", None):
                url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
                payload = {"chat_id": config.telegram_chat_id, "text": "daily_stock_analysis API smoke test"}
                if getattr(config, "telegram_message_thread_id", None):
                    payload["message_thread_id"] = config.telegram_message_thread_id
                response = requests.post(url, json=payload, timeout=timeout)
                action = "sendMessage"
            else:
                url = f"https://api.telegram.org/bot{telegram_token}/getMe"
                response = requests.get(url, timeout=timeout)
                action = "getMe"
            body = response.json()
            results.append(ApiCheckResult(category="notify", name="Telegram", configured=True, success=response.status_code == 200 and body.get("ok") is True, message=f"{action}: {str(body)[:260]}", details={"key": mask_secret(telegram_token)}))
        except Exception as exc:
            results.append(result_from_exception("notify", "Telegram", exc))

    pushover_user_key = getattr(config, "pushover_user_key", None)
    pushover_api_token = getattr(config, "pushover_api_token", None)
    if pushover_user_key or pushover_api_token:
        if not (pushover_user_key and pushover_api_token):
            results.append(ApiCheckResult(category="notify", name="Pushover", configured=True, success=False, message="PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN must both be configured"))
        else:
            try:
                response = requests.post(
                    "https://api.pushover.net/1/users/validate.json",
                    data={"token": pushover_api_token, "user": pushover_user_key},
                    timeout=timeout,
                )
                body = response.json()
                results.append(ApiCheckResult(category="notify", name="Pushover", configured=True, success=response.status_code == 200 and body.get("status") == 1, message=str(body)[:300]))
            except Exception as exc:
                results.append(result_from_exception("notify", "Pushover", exc))

    pushplus_token = getattr(config, "pushplus_token", None)
    if pushplus_token:
        if not send:
            results.append(ApiCheckResult(category="notify", name="PushPlus", configured=True, skipped=True, message="dry-run; pass --notify-send to send a test message", details={"key": mask_secret(pushplus_token)}))
        else:
            try:
                payload = {"token": pushplus_token, "title": "daily_stock_analysis API smoke test", "content": "API smoke test"}
                if getattr(config, "pushplus_topic", None):
                    payload["topic"] = config.pushplus_topic
                response = requests.post("https://www.pushplus.plus/send", json=payload, timeout=timeout)
                body = response.json()
                results.append(ApiCheckResult(category="notify", name="PushPlus", configured=True, success=response.status_code == 200 and body.get("code") == 200, message=str(body)[:300]))
            except Exception as exc:
                results.append(result_from_exception("notify", "PushPlus", exc))

    custom_urls = getattr(config, "custom_webhook_urls", []) or []
    if custom_urls:
        if not send:
            results.append(ApiCheckResult(category="notify", name="Custom Webhook", configured=True, skipped=True, message=f"dry-run for {len(custom_urls)} webhook(s); pass --notify-send to POST test payload"))
        else:
            headers = {"Content-Type": "application/json"}
            bearer = getattr(config, "custom_webhook_bearer_token", None)
            if bearer:
                headers["Authorization"] = f"Bearer {bearer}"
            for index, url in enumerate(custom_urls, start=1):
                try:
                    response = requests.post(url, headers=headers, json={"title": "daily_stock_analysis API smoke test", "content": "API smoke test"}, timeout=timeout)
                    results.append(ApiCheckResult(category="notify", name=f"Custom Webhook {index}", configured=True, success=200 <= response.status_code < 300, message=f"HTTP {response.status_code}: {response.text[:240]}"))
                except Exception as exc:
                    results.append(result_from_exception("notify", f"Custom Webhook {index}", exc))

    if not results:
        results.append(ApiCheckResult(category="notify", name="Notification APIs", configured=False, skipped=True, message="no notification API env vars configured"))
    return results


def format_results(results: Sequence[ApiCheckResult]) -> str:
    visible = [item for item in results if item.configured or item.skipped]
    category_width = max([len("category")] + [len(item.category) for item in visible])
    name_width = max([len("name")] + [len(item.name) for item in visible])
    lines = [f"{'status':<6} {'category':<{category_width}} {'name':<{name_width}} message"]
    lines.append(f"{'-' * 6} {'-' * category_width} {'-' * name_width} {'-' * 7}")
    for item in visible:
        lines.append(f"{item.status:<6} {item.category:<{category_width}} {item.name:<{name_width}} {redact_sensitive_text(item.message)}")
    return "\n".join(lines)


def summarize(results: Sequence[ApiCheckResult]) -> str:
    configured = [item for item in results if item.configured]
    failed = [item for item in configured if not item.skipped and not item.success]
    succeeded = [item for item in configured if item.success]
    skipped = [item for item in configured if item.skipped]
    return f"Summary: {len(succeeded)} OK, {len(failed)} FAIL, {len(skipped)} SKIP, {len(configured)} configured checks."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Test API credentials configured in .env.")
    parser.add_argument("--all", action="store_true", help="test all supported categories (default when no category flag is provided)")
    parser.add_argument("--llm", action="store_true", help="test LLM credentials/channels")
    parser.add_argument("--search", action="store_true", help="test search API credentials")
    parser.add_argument("--data", action="store_true", help="test market data API credentials")
    parser.add_argument("--fetch", action="store_true", help="legacy alias for --data")
    parser.add_argument("--graph", action="store_true", help="test Neo4j and Graphiti embedding configuration")
    parser.add_argument("--sentiment", action="store_true", help="test social sentiment API credentials")
    parser.add_argument("--notify", action="store_true", help="test notification API credentials")
    parser.add_argument("--notify-send", action="store_true", help="send real test messages for webhook-style notification channels")
    parser.add_argument("--all-keys", action="store_true", help="test every comma-separated key instead of only the first key per provider")
    parser.add_argument("--timeout", type=float, default=15.0, help="request timeout in seconds where supported")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--strict", action="store_true", help="exit with code 1 when any configured check fails")
    parser.add_argument("--verbose", action="store_true", help="enable INFO logging from underlying providers")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR, format="%(levelname)s: %(message)s")
    configure_proxy_from_env()

    categories = select_categories(args)
    results: List[ApiCheckResult] = []
    try:
        config = load_runtime_config()
    except Exception as exc:
        results.append(
            ApiCheckResult(
                category="config",
                name="Runtime config",
                configured=True,
                success=False,
                message=f"failed to load project config: {clean_error(exc)}",
            )
        )
        if args.json:
            print(json.dumps([item.to_dict() for item in results], ensure_ascii=False, indent=2))
        else:
            print(format_results(results))
            print()
            print("Summary: 0 OK, 1 FAIL, 0 SKIP, 1 configured checks.")
        return 1

    if "llm" in categories:
        results.extend(run_llm_checks(config, timeout=args.timeout, all_keys=args.all_keys))
    if "search" in categories:
        results.extend(run_search_checks(config, all_keys=args.all_keys))
    if "data" in categories:
        results.extend(run_data_checks(config, timeout=args.timeout))
    if "graph" in categories:
        results.extend(run_graph_checks(config, timeout=args.timeout))
    if "sentiment" in categories:
        results.extend(run_sentiment_checks(config))
    if "notify" in categories:
        results.extend(run_notify_checks(config, timeout=args.timeout, send=args.notify_send))

    if args.json:
        print(json.dumps([item.to_dict() for item in results], ensure_ascii=False, indent=2))
    else:
        print(format_results(results))
        print()
        print(summarize(results))

    has_failed_configured = any(item.configured and not item.skipped and not item.success for item in results)
    return 1 if args.strict and has_failed_configured else 0


if __name__ == "__main__":
    sys.exit(main())
