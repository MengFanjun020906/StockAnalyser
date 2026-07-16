# -*- coding: utf-8 -*-
"""Tests for compact evidence-card adapters."""

from src.agent.evidence import build_evidence_cards_for_stock, build_expert_packet
from api.v1.endpoints.agent import _extract_evidence_artifacts


def test_build_evidence_cards_compacts_capital_flow_and_orders_signals():
    cards = build_evidence_cards_for_stock(
        run_id="run-1",
        stock_code="603418",
        stock_name="友升股份",
        evidence={
            "get_capital_flow": {
                "status": "ok",
                "main_net_inflow": -12831600,
                "inflow_5d": -5000000,
                "latest_date": "2026-04-15",
                "source_chain": [{"provider": "tushare:moneyflow", "result": "ok"}],
            }
        },
    )

    assert len(cards) == 1
    card = cards[0]
    assert card.dimension == "capital_flow"
    assert card.data_quality.source == "tushare:moneyflow"
    assert card.data_quality.freshness in {"recent", "stale", "eod_current"}
    assert card.signals[0].name == "main_net_inflow"
    assert abs(card.signals[0].score_delta) >= abs(card.signals[-1].score_delta)
    assert card.impact.stance == "oppose"
    assert card.counter_evidence[0].refuted_claim


def test_build_expert_packet_degrades_missing_dimension():
    packet = build_expert_packet(
        run_id="run-1",
        expert="capital_chip_expert",
        dimension="capital_chip",
        cards=[],
        missing_hint="get_capital_flow/get_chip_distribution",
    )

    assert packet.stance == "invalid"
    assert packet.action_bias == "wait"
    assert packet.confidence == 0
    assert "get_capital_flow/get_chip_distribution" in packet.missing_evidence


def test_unavailable_tool_result_is_not_treated_as_evidence():
    cards = build_evidence_cards_for_stock(
        run_id="run-unavailable",
        stock_code="600519",
        stock_name="贵州茅台",
        evidence={
            "get_capital_flow": {
                "status": "unavailable",
                "data_available": False,
                "provider_errors": ["upstream unavailable"],
                "source_chain": [{"provider": "tushare:moneyflow", "result": "failed"}],
            }
        },
    )

    assert cards[0].data_quality.status == "unavailable"
    assert cards[0].impact.stance == "invalid"
    assert cards[0].impact.confidence == 0


def test_trace_artifact_extractor_lands_compact_evidence_packets():
    artifacts = _extract_evidence_artifacts({
        "expert_state": {
            "evidence_bundle": {
                "evidence_cards": [{"card_id": "capital_flow:603418:2026-04-15:get_capital_flow"}],
                "expert_packets": [{"expert": "capital_chip_expert"}],
                "judge_input_packet": {"decision_matrix": [{"dimension": "capital_chip"}]},
            }
        }
    })

    assert set(artifacts) == {"evidence_cards", "expert_packets", "judge_input_packet"}
    assert artifacts["evidence_cards"][0]["card_id"].startswith("capital_flow:")
    assert artifacts["expert_packets"][0]["expert"] == "capital_chip_expert"
    assert artifacts["judge_input_packet"]["decision_matrix"][0]["dimension"] == "capital_chip"


def test_trace_artifact_extractor_lands_balanced_candidate_evidence():
    artifacts = _extract_evidence_artifacts({
        "balanced_candidate_evidence": {
            "full": {
                "candidate_evidence_json": {
                    "schema_version": "candidate_evidence.v1",
                    "candidates": [{"code": "301028", "bucket": "strategy"}],
                },
                "candidate_evidence_md": "# 候选证据包\n\n| 类别 | 股票 |\n",
            }
        }
    })

    assert artifacts["candidate_evidence"]["schema_version"] == "candidate_evidence.v1"
    assert artifacts["candidate_evidence"]["candidates"][0]["code"] == "301028"
    assert artifacts["candidate_evidence.md"].startswith("# 候选证据包")
