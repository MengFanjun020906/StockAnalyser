"""Behavior tests for the AnySearch-backed search interface."""

import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.config import Config
from src.core.config_registry import get_field_definition
from src.search_service import (
    AnySearchProvider,
    SearchService,
    get_search_service,
    reset_search_service,
)


def test_anysearch_provider_posts_bearer_request_and_parses_nested_results() -> None:
    response = MagicMock()
    response.status_code = 200
    response.headers = {"content-type": "application/json; charset=utf-8"}
    response.json.return_value = {
        "code": 0,
        "message": "success",
        "data": {
            "results": [
                {
                    "title": "贵州茅台现金分红公告",
                    "snippet": "贵州茅台发布现金分红公告。",
                    "content": "2026-07-13 09:30:00 贵州茅台发布现金分红公告。",
                    "url": "https://finance.example.com/2026-07-13/notice.html",
                }
            ]
        },
    }

    with patch("src.search_service._post_with_retry", return_value=response) as post:
        result = AnySearchProvider(["secret-key"]).search(
            "贵州茅台 最新消息",
            max_results=5,
            days=3,
        )

    assert result.success is True
    assert result.provider == "AnySearch"
    assert len(result.results) == 1
    assert result.results[0].source == "finance.example.com"
    assert result.results[0].published_date == "2026-07-13"
    request = post.call_args
    assert request.args == ("https://api.anysearch.com/v1/search",)
    assert request.kwargs["headers"]["Authorization"] == "Bearer secret-key"
    assert request.kwargs["json"] == {
        "query": "贵州茅台 最新消息",
        "max_results": 5,
    }


def test_anysearch_provider_supports_tag_and_params_for_specialized_search() -> None:
    response = MagicMock()
    response.status_code = 200
    response.headers = {"content-type": "application/json"}
    response.json.return_value = {
        "code": 0,
        "message": "success",
        "data": {"results": []},
    }

    with patch("src.search_service._post_with_retry", return_value=response) as post:
        result = AnySearchProvider(["secret-key"]).search(
            "Go 1.26 release notes",
            max_results=10,
            tag="code.doc",
            params={"library": "golang"},
        )

    assert result.success is True
    assert post.call_args.kwargs["json"] == {
        "query": "Go 1.26 release notes",
        "tag": "code.doc",
        "params": {"library": "golang"},
        "max_results": 10,
    }


def test_search_service_uses_only_anysearch_when_key_is_configured() -> None:
    service = SearchService(
        anysearch_api_key="secret-key",
        bocha_keys=["legacy-bocha"],
        tavily_keys=["legacy-tavily"],
        searxng_base_urls=["https://legacy-search.example.com"],
    )

    assert len(service._providers) == 1
    assert isinstance(service._providers[0], AnySearchProvider)


def test_general_news_keeps_recent_anysearch_result_after_date_filtering() -> None:
    today = datetime.now().date().isoformat()
    response = MagicMock()
    response.status_code = 200
    response.headers = {"content-type": "application/json"}
    response.json.return_value = {
        "code": 0,
        "message": "success",
        "data": {
            "results": [
                {
                    "title": "半导体产业最新进展",
                    "snippet": f"{today} 半导体产业发布最新进展。",
                    "content": "",
                    "url": "https://news.example.com/latest.html",
                }
            ]
        },
    }
    service = SearchService(
        anysearch_api_key="secret-key",
        searxng_public_instances_enabled=False,
        news_max_age_days=3,
    )

    with patch("src.search_service._post_with_retry", return_value=response):
        result = service.search_general_news("半导体 最新进展", max_results=3, days=3)

    assert result.success is True
    assert result.provider == "AnySearch"
    assert [item.title for item in result.results] == ["半导体产业最新进展"]
    assert result.results[0].published_date == today


def test_config_reads_anysearch_key_as_the_search_capability() -> None:
    with patch.dict(os.environ, {"ANYSEARCH_API_KEY": "env-secret"}, clear=True):
        with patch.object(Config, "_parse_stock_email_groups", return_value=[]):
            config = Config._load_from_env()

    assert config.anysearch_api_key == "env-secret"
    assert config.has_search_capability_enabled() is True


def test_anysearch_key_is_exposed_as_sensitive_runtime_configuration() -> None:
    field = get_field_definition("ANYSEARCH_API_KEY")

    assert field is not None
    assert field["ui_control"] == "password"
    assert field["is_sensitive"] is True


def test_shared_search_service_uses_anysearch_from_config() -> None:
    config = SimpleNamespace(
        anysearch_api_key="env-secret",
        bocha_api_keys=[],
        tavily_api_keys=[],
        anspire_api_keys=[],
        brave_api_keys=[],
        serpapi_keys=[],
        minimax_api_keys=[],
        searxng_base_urls=[],
        searxng_public_instances_enabled=False,
        news_max_age_days=3,
        news_strategy_profile="short",
    )
    reset_search_service()
    try:
        with patch("src.config.get_config", return_value=config):
            service = get_search_service()
    finally:
        reset_search_service()

    assert len(service._providers) == 1
    assert isinstance(service._providers[0], AnySearchProvider)
