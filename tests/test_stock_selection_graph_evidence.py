"""Behavior tests for graph evidence used by stock-selection stages."""

from unittest.mock import MagicMock

from src.agent.stock_selection_graph import StockSelectionGraphEvidence
from src.agent.stock_selection_prompts import (
    build_adversarial_review_prompt,
    build_candidate_discovery_prompt,
)


def test_selection_graph_evidence_indexes_results_by_stock_code() -> None:
    graphiti = MagicMock()
    graphiti.search_sync.return_value = {
        "success": True,
        "source": "graphiti",
        "degraded": False,
        "episodes": [
            {"type": "analysis_history", "code": "600001", "analysis_summary": "历史结论偏强"},
            {
                "type": "news_signal_card",
                "card_id": "card:1",
                "summary_short": "订单事件",
                "company_impacts": [{"symbol": "600001", "name": "测试股票"}],
            },
        ],
        "edges": [{"name": "related", "quality_grade": "high"}],
        "nodes": [],
    }

    evidence = StockSelectionGraphEvidence(graphiti=graphiti, timeout_seconds=8).collect_discovery(
        task="寻找订单驱动候选",
        market="cn",
        target_symbols=["600001.SH"],
    )

    assert evidence["status"] == "ok"
    assert evidence["required"] is True
    assert len(evidence["by_code"]["600001"]) == 2
    graphiti.search_sync.assert_called_once()
    assert graphiti.search_sync.call_args.kwargs["timeout_seconds"] == 8


def test_selection_prompts_require_graph_evidence_audit_and_weak_edge_guardrail() -> None:
    graph_evidence = {
        "status": "partial",
        "degraded": True,
        "source": "relational_fallback",
        "items": [],
    }
    discovery = build_candidate_discovery_prompt({"knowledge_graph_evidence": graph_evidence})
    adversarial = build_adversarial_review_prompt({"knowledge_graph_evidence": graph_evidence})

    assert "必须先读取 knowledge_graph_evidence" in discovery
    assert "弱语义边不得作为因果" in discovery
    assert "必须审查 knowledge_graph_evidence" in adversarial
    assert "降级来源" in adversarial
