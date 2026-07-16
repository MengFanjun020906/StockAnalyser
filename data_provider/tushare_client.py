# -*- coding: utf-8 -*-
"""Shared Tushare Pro client initialization.

All project Tushare calls should import this module instead of hard-coding an
endpoint.  The project defaults to the configured private Tushare-compatible
gateway, while ``TUSHARE_HTTP_URL`` can still override it for deployments.
"""

from __future__ import annotations

import json
import os
import ipaddress
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from urllib.parse import urlparse
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import requests

DEFAULT_TUSHARE_HTTP_URL = "http://118.89.66.41:8010/"
_TUSHARE_QUERY_LOCK = threading.Lock()
_TUSHARE_HEALTH_LOCK = threading.Lock()
_TUSHARE_RUNTIME_HEALTH: Dict[str, Dict[str, Any]] = {}
_TUSHARE_CREDENTIAL_QUARANTINE_SECONDS = 900.0
_TUSHARE_CREDENTIAL_ERROR_MARKERS = (
    "token已过期",
    "token不对",
    "invalid token",
    "token invalid",
    "token expired",
)


class TushareQueryTimeout(TimeoutError):
    """Raised when a Tushare SDK call exceeds the caller budget."""


class TushareCredentialError(RuntimeError):
    """Raised when the configured Tushare credential is invalid or expired."""


def is_tushare_credential_error(error: Any) -> bool:
    text = str(error or "").strip().lower()
    return any(marker.lower() in text for marker in _TUSHARE_CREDENTIAL_ERROR_MARKERS)


def _tushare_health_key(token: str, api_url: str) -> str:
    raw = f"{str(token or '').strip()}|{str(api_url or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _mark_tushare_credential_invalid(token: str, api_url: str, error: Any) -> None:
    key = _tushare_health_key(token, api_url)
    with _TUSHARE_HEALTH_LOCK:
        _TUSHARE_RUNTIME_HEALTH[key] = {
            "status": "credential_invalid",
            "available": False,
            "reason": str(error or "Tushare credential invalid"),
            "unavailable_until": time.time() + _TUSHARE_CREDENTIAL_QUARANTINE_SECONDS,
        }


def _clear_tushare_runtime_health(token: str, api_url: str) -> None:
    with _TUSHARE_HEALTH_LOCK:
        _TUSHARE_RUNTIME_HEALTH.pop(_tushare_health_key(token, api_url), None)


def get_tushare_runtime_health(
    *,
    token: Optional[str] = None,
    api_url: Optional[str] = None,
) -> Dict[str, Any]:
    resolved_token = str(token if token is not None else get_tushare_token()).strip()
    resolved_url = str(api_url or get_tushare_http_url()).strip()
    key = _tushare_health_key(resolved_token, resolved_url)
    with _TUSHARE_HEALTH_LOCK:
        health = dict(_TUSHARE_RUNTIME_HEALTH.get(key) or {})
        if health and float(health.get("unavailable_until") or 0.0) <= time.time():
            _TUSHARE_RUNTIME_HEALTH.pop(key, None)
            health = {}
    if health:
        return health
    return {
        "status": "configured" if resolved_token else "not_configured",
        "available": bool(resolved_token),
        "reason": None if resolved_token else "TUSHARE_TOKEN is not configured",
        "unavailable_until": None,
    }


def reset_tushare_runtime_health() -> None:
    with _TUSHARE_HEALTH_LOCK:
        _TUSHARE_RUNTIME_HEALTH.clear()


def _raise_if_tushare_quarantined(token: str, api_url: str) -> None:
    health = get_tushare_runtime_health(token=token, api_url=api_url)
    if health.get("status") == "credential_invalid" and not health.get("available"):
        raise TushareCredentialError(str(health.get("reason") or "Tushare credential invalid"))


def get_tushare_token() -> str:
    return os.getenv("TUSHARE_TOKEN", "").strip()


def get_tushare_http_url() -> str:
    return (os.getenv("TUSHARE_HTTP_URL", "").strip() or DEFAULT_TUSHARE_HTTP_URL).rstrip("/") + "/"


def should_bypass_proxy_for_tushare_url(api_url: str) -> bool:
    """Bypass local proxy for direct Tushare/private-gateway HTTP endpoints."""
    host = (urlparse(api_url).hostname or "").lower()
    if not host:
        return False
    if host in {"api.tushare.pro", "tushare.pro"}:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host.startswith(("10.", "172.", "192.168.", "127.", "localhost")):
        return True
    return False


class TushareHttpClient:
    """Lightweight Tushare Pro HTTP client."""

    def __init__(self, token: str, timeout: int = 30, api_url: Optional[str] = None) -> None:
        self._token = token
        self._timeout = timeout
        self._api_url = (api_url or DEFAULT_TUSHARE_HTTP_URL).rstrip("/") + "/"

    @property
    def api_url(self) -> str:
        return self._api_url

    def query(self, api_name: str, fields: str = "", **kwargs) -> pd.DataFrame:
        _raise_if_tushare_quarantined(self._token, self._api_url)
        req_params = {
            "api_name": api_name,
            "token": self._token,
            "params": kwargs,
            "fields": fields,
        }
        request_kwargs: Dict[str, Any] = {"json": req_params, "timeout": self._timeout}
        if should_bypass_proxy_for_tushare_url(self._api_url):
            request_kwargs["proxies"] = {"http": None, "https": None}
        res = requests.post(self._api_url, **request_kwargs)
        if res.status_code != 200:
            raise Exception(f"Tushare API HTTP {res.status_code}")

        result = json.loads(res.text)
        if result.get("code") != 0:
            message = result.get("msg") or f"Tushare API error code {result.get('code')}"
            if is_tushare_credential_error(message):
                _mark_tushare_credential_invalid(self._token, self._api_url, message)
                raise TushareCredentialError(message)
            raise Exception(message)

        _clear_tushare_runtime_health(self._token, self._api_url)

        data = result.get("data") or {}
        columns = data.get("fields") or []
        items = data.get("items") or []
        return pd.DataFrame(items, columns=columns)

    def __getattr__(self, api_name: str):
        if api_name.startswith("_"):
            raise AttributeError(api_name)

        def caller(**kwargs) -> pd.DataFrame:
            return self.query(api_name, **kwargs)

        return caller


def build_tushare_http_client(token: Optional[str] = None, timeout: int = 30) -> TushareHttpClient:
    resolved_token = (token or get_tushare_token()).strip()
    if not resolved_token:
        raise ValueError("TUSHARE_TOKEN is not configured")
    return TushareHttpClient(token=resolved_token, timeout=timeout, api_url=get_tushare_http_url())


def build_tushare_sdk_client(token: Optional[str] = None) -> Tuple[Any, Any]:
    """Return ``(tushare_module, pro)`` using env token and endpoint override."""
    import tushare as ts

    resolved_token = (token or get_tushare_token()).strip()
    if not resolved_token:
        raise ValueError("TUSHARE_TOKEN is not configured")
    pro = ts.pro_api(resolved_token)
    pro._DataApi__http_url = get_tushare_http_url()
    return ts, pro


def query_tushare_api_via_sdk(
    api_name: str,
    params: Optional[Dict[str, Any]] = None,
    fields: str = "",
    timeout: int = 30,
) -> pd.DataFrame:
    token = get_tushare_token()
    api_url = get_tushare_http_url()
    if token:
        _raise_if_tushare_quarantined(token, api_url)
    ts, pro = build_tushare_sdk_client()
    kwargs: Dict[str, Any] = dict(params or {})
    if fields:
        kwargs["fields"] = fields

    def _call() -> pd.DataFrame:
        if api_name == "pro_bar":
            return ts.pro_bar(api=pro, **kwargs)
        return getattr(pro, api_name)(**kwargs)

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tushare-sdk")
    future = executor.submit(_call)
    try:
        return future.result(timeout=int(max(1, timeout)))
    except FutureTimeoutError as exc:
        future.cancel()
        raise TushareQueryTimeout(f"Tushare SDK call timed out after {timeout}s") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def query_tushare_api(api_name: str, params: Optional[Dict[str, Any]] = None, fields: str = "", timeout: int = 30) -> pd.DataFrame:
    with _TUSHARE_QUERY_LOCK:
        try:
            result = query_tushare_api_via_sdk(api_name, params=params, fields=fields, timeout=timeout)
            return result if result is not None else pd.DataFrame()
        except TushareQueryTimeout:
            raise
        except Exception as exc:
            if is_tushare_credential_error(exc):
                token = get_tushare_token()
                api_url = get_tushare_http_url()
                _mark_tushare_credential_invalid(token, api_url, exc)
                raise TushareCredentialError(str(exc)) from exc
            client = build_tushare_http_client(timeout=timeout)
            return client.query(api_name, fields=fields, **(params or {}))
