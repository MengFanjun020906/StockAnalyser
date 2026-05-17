# -*- coding: utf-8 -*-
"""Shared Tushare Pro client initialization.

All project Tushare calls should import this module instead of hard-coding the
official endpoint.  Some deployments use a private Tushare-compatible gateway
and require overriding the SDK's internal HTTP URL.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import requests

DEFAULT_TUSHARE_HTTP_URL = "http://api.tushare.pro"


def get_tushare_token() -> str:
    return os.getenv("TUSHARE_TOKEN", "").strip()


def get_tushare_http_url() -> str:
    return (os.getenv("TUSHARE_HTTP_URL", "").strip() or DEFAULT_TUSHARE_HTTP_URL).rstrip("/") + "/"


class TushareHttpClient:
    """Lightweight Tushare Pro HTTP client."""

    def __init__(self, token: str, timeout: int = 30, api_url: Optional[str] = None) -> None:
        self._token = token
        self._timeout = timeout
        self._api_url = (api_url or get_tushare_http_url()).rstrip("/") + "/"

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
        res = requests.post(self._api_url, json=req_params, timeout=self._timeout)
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


def query_tushare_api(api_name: str, params: Optional[Dict[str, Any]] = None, fields: str = "", timeout: int = 30) -> pd.DataFrame:
    client = build_tushare_http_client(timeout=timeout)
    return client.query(api_name, fields=fields, **(params or {}))
