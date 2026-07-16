#!/usr/bin/env python3
"""Online Graphiti smoke validation for analysis, news edges, and agent search."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import Config, get_config, setup_env  # noqa: E402
from src.storage import DatabaseManager  # noqa: E402


def _query_neo4j(query: str, **params: Any) -> List[Dict[str, Any]]:
    from neo4j import GraphDatabase

    cfg = get_config()
    driver = GraphDatabase.driver(
        cfg.graphiti_neo4j_uri,
        auth=(cfg.graphiti_neo4j_user, cfg.graphiti_neo4j_password or ""),
    )
    try:
        with driver.session() as session:
            result = session.run(query, **params)
            return [dict(record) for record in result]
    finally:
        driver.close()


def _build_smoke_news_payload(token: str) -> Dict[str, Any]:
    return {
        "status": "ok",
        "provider": "loop.graphiti.online_smoke",
        "results": [
            {
                "id": token,
                "title": f"{token} 日本MLCC厂商出口受限，国产替代窗口打开",
                "content": "日本MLCC厂商因出口管制供应受限，下游客户加速导入国产替代和二供认证。",
                "snippet": "MLCC海外供应受限，国产替代和二供认证加速。",
                "url": f"https://example.test/graphiti-smoke/{token}",
                "published_at": datetime.now(timezone.utc).isoformat(),
                "score": 1000.0,
                "rank": 1,
                "is_important": True,
                "subjects": [],
                "subject_names": [],
                "stocks": [],
            }
        ],
        "source_chain": [{"provider": "loop.graphiti.online_smoke", "result": "ok"}],
        "errors": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", default="loop_smoke", help="Isolated Graphiti market group for validation")
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    args = parser.parse_args()

    setup_env()
    token = f"loop_graphiti_smoke_{int(time.time())}"

    with tempfile.TemporaryDirectory(prefix="dsa-graphiti-smoke-") as tmpdir:
        os.environ["DATABASE_PATH"] = str(Path(tmpdir) / "graphiti_smoke.db")
        Config.reset_instance()
        DatabaseManager.reset_instance()

        from src.agent.tools.graph_tools import search_knowledge_graph_tool
        from src.services.graphiti import get_graphiti_service
        from src.services.news_signal_service import NewsSignalService

        graphiti = get_graphiti_service()
        if not graphiti.is_available():
            print(json.dumps({"status": "failed", "reason": "graphiti_unavailable"}, ensure_ascii=False))
            return 1

        group_id = graphiti._resolve_group_id(market=args.market)  # type: ignore[attr-defined]
        service = NewsSignalService()
        service._concept_mapping = {
            "MLCC": {
                "aliases": ["MLCC", "片式多层陶瓷电容"],
                "related_boards": ["被动元件", "电子元件"],
                "mapped_stocks": [],
            }
        }

        raw_rows, card_rows = service._build_from_cls(_build_smoke_news_payload(token))
        service.repo.upsert_raw_episodes(raw_rows)
        saved_cards = service.repo.upsert_cards(card_rows)
        signal_date = str(saved_cards[0]["signal_date"])
        edge_result = service.rebuild_edges(signal_date=signal_date, include_semantic=False)
        card = service.repo.get_card(saved_cards[0]["card_id"])
        edges = service.repo.list_edges(card_id=saved_cards[0]["card_id"], limit=100)
        edge_projection = graphiti.sync_news_signal_edges_sync(
            cards=[card],
            edges=edges,
            market=args.market,
        )

        explicit_edge_rows = _query_neo4j(
            """
            MATCH (c:NewsSignalCard {card_id: $card_id})-[r]->(t)
            WHERE r.group_id = $group_id
            RETURN c.card_id AS card_id, type(r) AS relation_type,
                   r.edge_quality AS edge_quality, labels(t) AS target_labels,
                   coalesce(t.target_id, t.card_id, t.name) AS target_id
            LIMIT 20
            """,
            card_id=saved_cards[0]["card_id"],
            group_id=group_id,
        )

        analysis_code = f"SMOKE{int(time.time())}"
        analysis_episode_name = f"analysis:{analysis_code}:{datetime.now(timezone.utc).date().isoformat()}"
        graphiti.ingest_analysis_sync(
            code=analysis_code,
            stock_name="Graphiti在线验证",
            report_type="online_smoke",
            result={
                "analysis_summary": f"{token} 完整分析入图烟测：MLCC海外供应受限与国产替代链路。",
                "operation_advice": "validation_only",
                "trend_prediction": "validation_only",
            },
            context={"smoke_token": token, "market": args.market},
            news_context="MLCC海外供应受限，国产替代和二供认证加速。",
            market=args.market,
            user_id="loop-online-smoke",
        )
        analysis_rows = _query_neo4j(
            """
            MATCH (e:Episodic {name: $name, group_id: $group_id})
            RETURN count(e) AS episode_count
            """,
            name=analysis_episode_name,
            group_id=group_id,
        )

        tool_result = search_knowledge_graph_tool.handler(
            f"{token} MLCC 国产替代",
            market=args.market,
            limit=5,
        )
        tool_context_count = sum(
            len(tool_result.get(key) or [])
            for key in ("episodes", "edges", "nodes")
            if isinstance(tool_result.get(key), list)
        )

        output = {
            "status": "ok",
            "market": args.market,
            "group_id": group_id,
            "token": token,
            "news_card_id": saved_cards[0]["card_id"],
            "edge_rebuild": edge_result,
            "edge_projection": edge_projection,
            "neo4j_explicit_edge_count": len(explicit_edge_rows),
            "neo4j_explicit_edge_sample": explicit_edge_rows[:3],
            "analysis_episode_name": analysis_episode_name,
            "analysis_episode_count": int((analysis_rows[0] if analysis_rows else {}).get("episode_count") or 0),
            "agent_tool": {
                "success": bool(tool_result.get("success")),
                "source": tool_result.get("source"),
                "degraded": bool(tool_result.get("degraded", False)),
                "context_count": tool_context_count,
                "fallback_reason": tool_result.get("fallback_reason"),
                "error": tool_result.get("error") or tool_result.get("graphiti_error"),
            },
        }
        required_ok = (
            str(edge_projection.get("status") or "") == "ok"
            and len(explicit_edge_rows) > 0
            and output["analysis_episode_count"] > 0
            and output["agent_tool"]["success"] is True
            and output["agent_tool"]["context_count"] > 0
        )
        if not required_ok:
            output["status"] = "failed"
            print(json.dumps(output, ensure_ascii=False, default=str))
            return 1
        print(json.dumps(output, ensure_ascii=False, default=str))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
