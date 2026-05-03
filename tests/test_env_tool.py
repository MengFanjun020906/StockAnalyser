# -*- coding: utf-8 -*-
"""Unit tests for the `.env` API smoke-test helper."""

import argparse

import test_env


def test_mask_secret_keeps_only_edges():
    assert test_env.mask_secret("abcd12345678wxyz") == "abcd...wxyz"
    assert test_env.mask_secret("short") == "*****"
    assert test_env.mask_secret("") == ""


def test_redact_sensitive_query_values():
    text = "https://example.test/search?api_key=abcd12345678wxyz&token=short&x=1"
    redacted = test_env.redact_sensitive_text(text)
    assert "abcd12345678wxyz" not in redacted
    assert "token=short" not in redacted
    assert "api_key=abcd...wxyz" in redacted
    assert "token=*****" in redacted


def test_result_status_values():
    assert test_env.ApiCheckResult("llm", "x", configured=True, success=True).status == "OK"
    assert test_env.ApiCheckResult("llm", "x", configured=True, success=False).status == "FAIL"
    assert test_env.ApiCheckResult("llm", "x", configured=True, skipped=True).status == "SKIP"
    assert test_env.ApiCheckResult("llm", "x", configured=False).status == "UNSET"


def test_select_categories_defaults_to_all():
    args = argparse.Namespace(all=False, llm=False, search=False, data=False, fetch=False, sentiment=False, notify=False)
    assert test_env.select_categories(args) == set(test_env.DEFAULT_CATEGORIES)


def test_select_categories_uses_explicit_flags():
    args = argparse.Namespace(all=False, llm=True, search=False, data=True, fetch=False, sentiment=False, notify=False)
    assert test_env.select_categories(args) == {"llm", "data"}


def test_select_categories_supports_fetch_alias():
    args = argparse.Namespace(all=False, llm=False, search=False, data=False, fetch=True, sentiment=False, notify=False)
    assert test_env.select_categories(args) == {"data"}


def test_selected_keys_defaults_to_first_key():
    assert test_env.selected_keys(["a", "b"], all_keys=False) == ["a"]
    assert test_env.selected_keys(["a", "b"], all_keys=True) == ["a", "b"]
