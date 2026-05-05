# -*- coding: utf-8 -*-
"""Graphiti knowledge-graph agent tools."""

from __future__ import annotations

import logging

from src.agent.tools.registry import ToolDefinition, ToolParameter
from src.services.graphiti import get_graphiti_service

logger = logging.getLogger(__name__)


def _handle_search_knowledge_graph(query: str, market: str = "cn", limit: int = 10) -> dict:
    service = get_graphiti_service()
    result = service.search_sync(query, market=market, limit=limit)
    if not result.get("success"):
        return result
    return result


search_knowledge_graph_tool = ToolDefinition(
    name="search_knowledge_graph",
    description=(
        "Search the temporal knowledge graph for stock, event, sector, and analysis relationships. "
        "Use it to retrieve historical analysis conclusions and related event context."
    ),
    parameters=[
        ToolParameter(
            name="query",
            type="string",
            description="Search query, e.g. '贵州茅台 最近一个月 分析结论'",
        ),
        ToolParameter(
            name="market",
            type="string",
            description="Market partition, e.g. 'cn', 'hk', or 'us'",
            required=False,
            default="cn",
        ),
        ToolParameter(
            name="limit",
            type="integer",
            description="Maximum number of results to return",
            required=False,
            default=10,
        ),
    ],
    handler=_handle_search_knowledge_graph,
    category="search",
)


ALL_GRAPH_TOOLS = [search_knowledge_graph_tool]
