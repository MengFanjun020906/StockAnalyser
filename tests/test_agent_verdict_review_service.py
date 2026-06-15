# -*- coding: utf-8 -*-

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app
from src.config import Config
from src.services.agent_verdict_review_service import AgentVerdictReviewService
from src.storage import DatabaseManager, StockDaily


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_agent_verdict_review_builds_rows_from_trace_and_stock_daily():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATABASE_PATH"] = str(Path(tmp) / "test.db")
        Config._instance = None
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        trace_dir = Path(tmp) / "agent_traces" / "20260101-trace-a"
        trace_dir.mkdir(parents=True)
        _write_json(trace_dir / "summary.json", {"artifact_dir": str(trace_dir)})
        _write_json(trace_dir / "request.json", {"message": "帮我选股", "report_intent": "watchlist_scan"})
        _write_json(
            trace_dir / "final_report.json",
            {
                "selection_context": {"user_message": "帮我选股"},
                "market_regime": {"as_of": "2024-01-01", "regime": "range_bound", "risk_level": "medium"},
                "candidate_discovery": {"summary": {"candidate_codes": ["600001"]}},
                "portfolio_allocation": {
                    "full": {
                        "positions_plan": [
                            {"rank": 1, "code": "600001", "name": "测试一", "action": "wait"}
                        ]
                    }
                },
                "judge_decision": {
                    "summary": {
                        "primary_plan_verdict": "accept_with_changes",
                        "final_action": "wait",
                        "confidence": 0.62,
                    }
                },
            },
        )
        with db.get_session() as session:
            session.add(StockDaily(code="600001", date=date(2024, 1, 1), close=10.0, high=10.2, low=9.8))
            session.add(StockDaily(code="600001", date=date(2024, 1, 2), close=10.5, high=10.6, low=10.0))
            session.add(StockDaily(code="600001", date=date(2024, 1, 3), close=10.8, high=10.9, low=10.4))
            session.commit()

        service = AgentVerdictReviewService(db_manager=db)
        rows = service.build_reviews_for_trace(trace_dir=trace_dir, eval_windows=[2])

        assert len(rows) == 1
        row = rows[0]
        assert row["trace_id"] == "20260101-trace-a"
        assert row["chain_type"] == "stock_selection"
        assert row["decision_date"] == "2024-01-01"
        assert row["symbol"] == "600001"
        assert row["symbol_action"] == "wait"
        assert row["regime"] == "range_bound"
        assert row["windows"]["2"]["eval_status"] == "completed"
        assert row["windows"]["2"]["future_return_pct"] == 8.0
        assert row["review_label"] == "missed_up"

        output = Path(tmp) / "reviews.jsonl"
        result = service.write_reviews(rows, output_path=output)
        assert result.review_count == 1
        assert output.exists()
        assert json.loads(output.read_text(encoding="utf-8").strip())["review_label"] == "missed_up"

        DatabaseManager.reset_instance()


def test_agent_verdict_review_builds_single_stock_trace_shape():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATABASE_PATH"] = str(Path(tmp) / "test.db")
        Config._instance = None
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        trace_dir = Path(tmp) / "agent_traces" / "20260103-single-stock"
        trace_dir.mkdir(parents=True)
        _write_json(
            trace_dir / "request.json",
            {"stock_code": "600519", "stock_name": "贵州茅台", "report_intent": "single_stock_analysis"},
        )
        _write_json(
            trace_dir / "risk_gate.json",
            {"trade_plan": {"symbol": "600519", "action": "hold"}, "risk_gate": {"allowed_action": "hold"}},
        )
        _write_json(
            trace_dir / "final_report.json",
            {
                "stock_name": "贵州茅台",
                "operation_advice": "观望",
                "decision_type": "hold",
                "confidence_level": "中",
                "analysis_date": "2024-01-01",
                "dashboard": {"core_conclusion": {"one_sentence": "等待"}},
            },
        )
        with db.get_session() as session:
            session.add(StockDaily(code="600519", date=date(2024, 1, 1), close=100.0, high=101.0, low=99.0))
            session.add(StockDaily(code="600519", date=date(2024, 1, 2), close=103.0, high=104.0, low=100.0))
            session.commit()

        rows = AgentVerdictReviewService(db_manager=db).build_reviews_for_trace(trace_dir=trace_dir, eval_windows=[1])

        assert len(rows) == 1
        assert rows[0]["chain_type"] == "single_stock_analysis"
        assert rows[0]["symbol"] == "600519"
        assert rows[0]["name"] == "贵州茅台"
        assert rows[0]["symbol_action"] == "hold"
        assert rows[0]["operation_advice"] == "观望"
        assert rows[0]["decision_type"] == "hold"
        assert rows[0]["confidence"] == 0.55
        assert rows[0]["windows"]["1"]["future_return_pct"] == 3.0
        assert rows[0]["review_label"] == "hit"

        DatabaseManager.reset_instance()


def test_agent_verdict_review_marks_insufficient_data_without_forcing_label():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATABASE_PATH"] = str(Path(tmp) / "test.db")
        Config._instance = None
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        trace_dir = Path(tmp) / "agent_traces" / "20260102-trace-b"
        trace_dir.mkdir(parents=True)
        _write_json(
            trace_dir / "final_report.json",
            {
                "market_regime": {"as_of": "2024-01-01", "regime": "risk_off"},
                "candidate_discovery": {"summary": {"candidate_codes": ["600002"]}},
                "portfolio_allocation": {"full": {"positions_plan": []}},
                "judge_decision": {"summary": {"final_action": "open", "primary_plan_verdict": "accept"}},
            },
        )

        rows = AgentVerdictReviewService(db_manager=db).build_reviews_for_trace(trace_dir=trace_dir, eval_windows=[7])

        assert len(rows) == 1
        assert rows[0]["symbol"] == "600002"
        assert rows[0]["review_label"] == "insufficient_data"
        assert rows[0]["data_quality"] == "insufficient_price"
        assert rows[0]["limits"] == ["missing_start_bar"]

        DatabaseManager.reset_instance()


def test_agent_verdict_review_query_filters_and_summarizes_jsonl():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "verdict_review.jsonl"
        rows = [
            {
                "chain_type": "stock_selection",
                "trace_id": "trace-a",
                "decision_date": "2024-01-02",
                "symbol": "600001",
                "review_label": "missed_up",
                "windows": {"7": {"eval_status": "completed", "future_return_pct": 5.0}},
            },
            {
                "chain_type": "single_stock_analysis",
                "trace_id": "trace-b",
                "decision_date": "2024-01-01",
                "symbol": "600519",
                "review_label": "hit",
                "windows": {"7": {"eval_status": "completed", "future_return_pct": 3.0}},
            },
        ]
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")

        result = AgentVerdictReviewService().query_reviews(input_path=path, chain_type="stock_selection")

        assert result["exists"] is True
        assert result["total"] == 1
        assert result["items"][0]["trace_id"] == "trace-a"
        assert result["summary"]["chain_counts"]["stock_selection"] == 1
        assert result["summary"]["label_counts"]["missed_up"] == 1
        assert result["summary"]["completion_rate_pct"] == 100
        assert result["summary"]["avg_future_return_pct"] == 5.0


def test_agent_verdict_review_builds_stable_insight_markdown():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "verdict_review.jsonl"
        output = Path(tmp) / "insights" / "agent_verdict_insights.md"
        rows = [
            {
                "chain_type": "stock_selection",
                "trace_id": f"trace-{idx}",
                "decision_date": "2024-01-01",
                "symbol": f"60000{idx}",
                "symbol_action": "wait",
                "regime": "range_bound",
                "review_label": "missed_up",
                "windows": {"30": {"eval_status": "completed", "future_return_pct": 5.0 + idx}},
            }
            for idx in range(3)
        ]
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")

        result = AgentVerdictReviewService().build_insight_markdown(
            input_path=path,
            output_path=output,
            min_samples=3,
            top_n=6,
        )

        assert result.row_count == 3
        assert result.completed_count == 3
        assert result.stable_insight_count >= 1
        assert output.exists()
        markdown = output.read_text(encoding="utf-8")
        assert "Agent Verdict Review Insights" in markdown
        assert "防守/等待后踏空样本偏多" in markdown
        assert "本文件仅用于离线复盘" in markdown
        assert "不自动注入线上 Agent" in markdown


def test_agent_verdict_review_insight_markdown_respects_min_samples():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "verdict_review.jsonl"
        output = Path(tmp) / "insights.md"
        rows = [
            {
                "chain_type": "stock_selection",
                "trace_id": "trace-a",
                "decision_date": "2024-01-01",
                "symbol": "600001",
                "symbol_action": "wait",
                "review_label": "missed_up",
                "windows": {"30": {"eval_status": "completed", "future_return_pct": 6.0}},
            }
        ]
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")

        result = AgentVerdictReviewService().build_insight_markdown(
            input_path=path,
            output_path=output,
            min_samples=5,
        )

        assert result.row_count == 1
        assert result.stable_insight_count == 0
        markdown = output.read_text(encoding="utf-8")
        assert "暂无达到阈值的稳定洞察" in markdown
        assert "不适合沉淀为长期提示" in markdown


def test_agent_verdict_review_rebuild_api_uses_local_traces_and_writes_jsonl():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        db_path = data_dir / "test.db"
        env_path = data_dir / ".env"
        env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600001",
                    "GEMINI_API_KEY=test",
                    "ADMIN_AUTH_ENABLED=false",
                    f"DATABASE_PATH={db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.environ["ENV_FILE"] = str(env_path)
        os.environ["DATABASE_PATH"] = str(db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()

        trace_dir = data_dir / "agent_traces" / "20260104-api-trace"
        trace_dir.mkdir(parents=True)
        _write_json(trace_dir / "summary.json", {"artifact_dir": str(trace_dir)})
        _write_json(
            trace_dir / "final_report.json",
            {
                "market_regime": {"as_of": "2024-01-01", "regime": "range_bound"},
                "candidate_discovery": {"summary": {"candidate_codes": ["600001"]}},
                "portfolio_allocation": {"full": {"positions_plan": [{"code": "600001", "action": "wait"}]}},
                "judge_decision": {"summary": {"final_action": "wait", "primary_plan_verdict": "accept"}},
            },
        )
        with db.get_session() as session:
            session.add(StockDaily(code="600001", date=date(2024, 1, 1), close=10.0, high=10.1, low=9.9))
            session.add(StockDaily(code="600001", date=date(2024, 1, 2), close=10.6, high=10.7, low=10.0))
            session.commit()

        output = data_dir / "verdict_review.jsonl"
        client = TestClient(create_app(static_dir=data_dir / "empty-static"))
        with patch.object(AgentVerdictReviewService, "default_output_path", staticmethod(lambda: output)):
            response = client.post("/api/v1/agent-verdict-reviews/rebuild", params={"windows": "1", "limit": 10})

        assert response.status_code == 200
        payload = response.json()
        assert payload["trace_count"] == 1
        assert payload["review_count"] == 1
        assert payload["eval_windows"] == [1]
        assert Path(payload["output_path"]) == output
        assert output.exists()
        row = json.loads(output.read_text(encoding="utf-8").strip())
        assert row["chain_type"] == "stock_selection"
        assert row["review_label"] == "missed_up"

        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
