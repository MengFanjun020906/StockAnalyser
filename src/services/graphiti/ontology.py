# -*- coding: utf-8 -*-
"""Graphiti ontology definitions for stock-analysis episodes."""

from pydantic import BaseModel, Field
from graphiti_core.nodes import EntityNode


class Stock(BaseModel):
    """上市公司股票。"""

    code: str = Field(description="股票代码")
    stock_name: str = Field(description="股票名称")
    market: str = Field(description="市场：cn / hk / us")


class Sector(BaseModel):
    """行业或概念板块。"""

    sector_name: str = Field(description="板块名称")
    level: str = Field(description="板块层级：industry / concept / theme")


class MarketEvent(BaseModel):
    """市场事件。"""

    title: str = Field(description="事件标题")
    event_type: str = Field(description="事件类型：policy / earnings / macro / corporate / geopolitical")


class AnalysisConclusion(BaseModel):
    """分析结论。"""

    signal: str = Field(description="信号：buy / sell / hold / strong_buy / strong_sell")
    sentiment_score: int = Field(description="情绪评分 0-100")
    confidence: str = Field(description="置信度：high / medium / low")


DEFAULT_ENTITY_TYPES = {
    "Stock": Stock,
    "Sector": Sector,
    "MarketEvent": MarketEvent,
    "AnalysisConclusion": AnalysisConclusion,
}


def validate_entity_types(entity_types: dict[str, type[BaseModel]]) -> None:
    """Reject ontology fields that collide with Graphiti's protected entity fields."""

    protected_fields = set(EntityNode.model_fields.keys())
    for entity_type_name, model in entity_types.items():
        for field_name in model.model_fields.keys():
            if field_name in protected_fields:
                raise ValueError(
                    f"{entity_type_name}.{field_name} conflicts with Graphiti protected field names",
                )


validate_entity_types(DEFAULT_ENTITY_TYPES)
