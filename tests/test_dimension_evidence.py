# -*- coding: utf-8 -*-
"""Regression tests for fundamental evidence that looks successful but is empty."""

import json
import unittest

from src.agent.dimension_evidence import (
    build_dimension_evidence_summary,
    has_readable_fundamental_numbers,
    is_unreadable_fundamental_payload,
)
from src.agent.evidence.adapter import build_evidence_cards_for_stock


def _call(tool, payload, success=True):
    return {
        "tool": tool,
        "success": success,
        "arguments": {"stock_code": "600410"},
        "result_preview": json.dumps(payload, ensure_ascii=False),
    }


_ZERO_INDICATORS = {
    "status": "ok",
    "items": [
        {
            "ts_code": "600410.SH",
            "end_date": "20260930",
            "revenue": 0,
            "net_profit": 0.0,
            "grossprofit_margin": "0",
            "netprofit_margin": None,
            "eps": 0,
        }
    ],
}

_REAL_INDICATORS = {
    "status": "ok",
    "items": [{"ts_code": "600410.SH", "revenue": 5123456789.0, "netprofit_margin": 8.6, "eps": 0.42}],
}


class FundamentalPlaceholderTestCase(unittest.TestCase):
    def test_placeholder_zero_payload_is_not_readable(self) -> None:
        self.assertIs(has_readable_fundamental_numbers(_ZERO_INDICATORS), False)
        self.assertIs(has_readable_fundamental_numbers(_REAL_INDICATORS), True)

    def test_payload_without_financial_fields_keeps_default(self) -> None:
        business_context = {
            "status": "ok",
            "name": "华胜天成",
            "industry": "软件服务",
            "description": "公司主营云计算与系统集成。",
        }
        self.assertIsNone(has_readable_fundamental_numbers(business_context))

    def test_nested_statement_rows_are_checked(self) -> None:
        payload = {
            "status": "ok",
            "statements": {"income": [{"revenue": 0, "n_income": "0.00"}]},
        }
        self.assertIs(has_readable_fundamental_numbers(payload), False)

    def test_zero_placeholder_tool_does_not_count_as_available(self) -> None:
        summary = build_dimension_evidence_summary(
            [_call("get_tushare_financial_indicators", _ZERO_INDICATORS)]
        )

        fundamental = summary["fundamental"]
        self.assertEqual(fundamental["quality"], "unavailable")
        self.assertEqual(fundamental["available_tools"], [])
        self.assertEqual(fundamental["failed_tools"], ["get_tushare_financial_indicators"])
        self.assertEqual(fundamental["missing_evidence"], ["fundamental_evidence_unavailable"])

    def test_real_numbers_keep_fundamental_quality_ok(self) -> None:
        summary = build_dimension_evidence_summary(
            [
                _call("get_tushare_financial_indicators", _REAL_INDICATORS),
                _call("get_tushare_daily_basic", {"status": "ok", "items": [{"pe_ttm": 21.4, "pb": 1.9}]}),
            ]
        )

        fundamental = summary["fundamental"]
        self.assertEqual(fundamental["quality"], "ok")
        self.assertEqual(fundamental["failed_tools"], [])

    def test_business_context_only_payload_is_not_downgraded(self) -> None:
        summary = build_dimension_evidence_summary(
            [
                _call("get_stock_business_context", {"status": "ok", "industry": "软件服务", "description": "云计算"}),
                _call("get_tushare_daily_basic", {"status": "ok", "items": [{"pe_ttm": 21.4}]}),
            ]
        )

        self.assertEqual(summary["fundamental"]["quality"], "ok")

    def test_selection_fallback_marks_placeholder_fundamental_as_missing(self) -> None:
        from src.agent.stock_selection import _selection_fundamental_dimension_summary

        summary = _selection_fundamental_dimension_summary(
            {"get_tushare_financial_indicators": _ZERO_INDICATORS},
            set(),
        )

        self.assertEqual(summary["verdict"], "missing")

    def test_selection_fallback_keeps_real_fundamental_neutral(self) -> None:
        from src.agent.stock_selection import _selection_fundamental_dimension_summary

        summary = _selection_fundamental_dimension_summary(
            {"get_tushare_financial_indicators": _REAL_INDICATORS},
            set(),
        )

        self.assertEqual(summary["verdict"], "neutral")

    def test_empty_rows_are_unreadable_for_row_endpoints(self) -> None:
        empty_rows = {"status": "ok", "trade_date": "20261124", "items": []}
        self.assertTrue(is_unreadable_fundamental_payload("get_tushare_financial_indicators", empty_rows))
        self.assertTrue(is_unreadable_fundamental_payload("get_tushare_forecast", empty_rows))
        self.assertFalse(is_unreadable_fundamental_payload("get_stock_info", {"status": "ok", "name": "华胜天成"}))

    def test_evidence_card_marks_zero_payload_as_empty(self) -> None:
        cards = build_evidence_cards_for_stock(
            run_id="test-run",
            stock_code="600410",
            evidence={"get_tushare_financial_indicators": _ZERO_INDICATORS},
        )

        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card.data_quality.status, "empty")
        self.assertEqual(card.impact.stance, "invalid")
        self.assertLessEqual(card.signals[0].score_delta, 0)
        self.assertIn("revenue", card.data_quality.missing_fields)

    def test_evidence_card_keeps_real_payload_ok(self) -> None:
        cards = build_evidence_cards_for_stock(
            run_id="test-run",
            stock_code="600410",
            evidence={"get_tushare_financial_indicators": _REAL_INDICATORS},
        )

        self.assertEqual(cards[0].data_quality.status, "ok")
        self.assertEqual(cards[0].data_quality.missing_fields, [])


if __name__ == "__main__":
    unittest.main()
