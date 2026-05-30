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
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from urllib.parse import urlparse
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import requests

DEFAULT_TUSHARE_HTTP_URL = "http://118.89.66.41:8010/"
_TUSHARE_QUERY_LOCK = threading.Lock()


class TushareQueryTimeout(TimeoutError):
    """Raised when a Tushare SDK call exceeds the caller budget."""


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
            raise Exception(result.get("msg") or f"Tushare API error code {result.get('code')}")

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
        except Exception:
            client = build_tushare_http_client(timeout=timeout)
            return client.query(api_name, fields=fields, **(params or {}))
