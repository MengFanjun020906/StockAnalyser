# -*- coding: utf-8 -*-

from copy import deepcopy

from src.agent.trade_plan_guard import (
    compute_atr20_pct,
    extract_price_range,
    extract_single_price,
    validate_plan_fields,
    validate_price_plan,
)


def test_zero_width_zone_is_warning_only():
    result = validate_price_plan(entry_text="回踩 65.5", stop_text="跌破 63.0", atr_pct=4.0)

    assert result["status"] == "ok"
    assert result["observed"]["entry_zone_low"] == 65.5
    assert result["observed"]["entry_zone_high"] == 65.5
    assert result["suggestions"]["entry_zone_high"] > 65.5
    assert result["suggestions"]["advisory_only"] is True
    codes = {item["code"] for item in result["issues"]}
    assert "entry_zone_zero_or_narrow" in codes
    assert "stop_loss_at_or_above_entry_low" not in codes


def test_stop_equal_entry_low_is_invalid_without_rewrite():
    result = validate_price_plan(entry_text="回踩 65.5", stop_text="跌破 65.5")

    assert result["status"] == "invalid"
    assert result["observed"]["stop_loss_price"] == 65.5
    assert result["observed"]["entry_zone_low"] == 65.5
    assert result["suggestions"]["stop_loss"] < 65.5
    assert "stop_loss_at_or_above_entry_low" in {item["code"] for item in result["issues"]}


def test_stop_above_entry_high_is_rejected():
    result = validate_price_plan(entry_text="回踩 65.5-67.5", stop_text="跌破 68.0", atr_pct=5.0)

    assert result["status"] == "invalid"
    assert result["observed"]["stop_loss_price"] == 68.0
    assert "stop_loss_above_entry_zone" in {item["code"] for item in result["issues"]}


def test_atr_decoupled_stop_is_warning_only():
    result = validate_price_plan(entry_text="100-102", stop_text="跌破 99.9", atr_pct=2.0)

    assert result["status"] == "ok"
    assert result["observed"]["stop_loss_price"] == 99.9
    assert result["suggestions"]["stop_loss"] == 99.0
    issues = {item["code"]: item for item in result["issues"]}
    assert issues["stop_loss_distance_out_of_reference_range"]["severity"] == "warning"
    assert issues["stop_loss_distance_out_of_reference_range"]["reference"]["min_pct"] == 1.0


def test_extreme_atr_keeps_stop_suggestion_inside_absolute_cap():
    result = validate_price_plan(entry_text="100-102", stop_text="跌破 90.0", atr_pct=20.0)

    assert result["status"] == "ok"
    assert result["observed"]["stop_loss_price"] == 90.0
    assert result["suggestions"]["stop_loss"] == 92.0
    issue = next(item for item in result["issues"] if item["code"] == "stop_loss_distance_out_of_reference_range")
    assert issue["reference"]["max_pct"] == 8.0


def test_unparseable_entry_needs_review():
    result = validate_price_plan(entry_text="价格缺失", stop_text="跌破 10.0")

    assert result["status"] == "needs_review"
    assert "entry_zone_unparseable" in {item["code"] for item in result["issues"]}
    assert result["observed"]["entry_zone_low"] is None


def test_non_positive_and_inverted_zones_are_invalid():
    non_positive = validate_price_plan(entry_text="0-10", stop_text="跌破 0.5")
    inverted = validate_price_plan(
        entry_low=102.0,
        entry_high=100.0,
        stop_loss=99.0,
    )

    assert non_positive["status"] == "invalid"
    assert "entry_zone_non_positive" in {item["code"] for item in non_positive["issues"]}
    assert inverted["status"] == "invalid"
    assert "entry_zone_order" in {item["code"] for item in inverted["issues"]}


def test_validate_plan_fields_does_not_mutate_plan():
    plan = {
        "code": "600519",
        "entry_condition": "回踩 65.5",
        "stop_loss_condition": "跌破 65.5",
        "entry_zone_low": 65.5,
        "entry_zone_high": 65.5,
        "stop_loss_price": 65.5,
    }
    before = deepcopy(plan)

    result = validate_plan_fields(
        plan,
        entry_field="entry_condition",
        stop_field="stop_loss_condition",
        atr_pct=None,
    )

    assert plan == before
    assert result["status"] == "invalid"
    assert result["advisory_only"] is True
    assert result["observed"] == {
        "entry_zone_low": 65.5,
        "entry_zone_high": 65.5,
        "stop_loss_price": 65.5,
    }


def test_compute_atr20_pct_from_records():
    records = [
        {"high": 11.0, "low": 9.0, "close": 10.0, "pre_close": 10.0}
        for _ in range(21)
    ]

    assert compute_atr20_pct(records) == 20.0
    assert compute_atr20_pct(records[:5]) == 20.0
    assert compute_atr20_pct([]) is None


def test_extract_price_range_skips_percentages_and_indicators():
    assert extract_price_range("上涨 3% 后回踩 10.0-10.2") == (10.0, 10.2)
    assert extract_price_range("跌破 65.50（结构失效）") == (65.5, 65.5)
    assert extract_single_price("RSI20 附近") is None
