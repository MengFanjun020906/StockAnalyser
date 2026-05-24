# -*- coding: utf-8 -*-
"""Pydantic schema for per-dimension tool manifest.

A manifest entry adds *business semantics* to a tool that already exists in the
global `ToolRegistry`. ToolDefinition keeps the canonical name + JSON Schema for
parameters; this layer answers "WHEN should I call it, WHAT will I get back,
HOW does it combine with other tools" — questions the registry-level English
one-liner doesn't cover.

Locked field set (9):
    tool, priority, when_to_use, typical_args, returns_summary,
    key_fields, combo_hints, cost, failure_modes

Same template is reused by sentiment.yaml / fundamental.yaml / technical.yaml.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


CostLevel = Literal["cheap", "medium", "expensive"]


class ManifestEntry(BaseModel):
    """Business semantics for a single tool inside one expert dimension."""

    tool: str = Field(
        ...,
        description="Tool name; must exist in global ToolRegistry and the expert whitelist.",
    )
    priority: int = Field(
        ...,
        ge=1,
        le=99,
        description="Lower = higher priority. Used to sort tool docs in prompt.",
    )
    when_to_use: str = Field(
        ...,
        description="Plain-language guide for when this tool is the right choice.",
    )
    typical_args: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Example invocation arguments. Values may contain runtime placeholders "
            "like {today} or {seed_codes}; the renderer substitutes them at prompt "
            "build time. Keys MUST match ToolDefinition.parameters names."
        ),
    )
    returns_summary: str = Field(
        ...,
        description="One-paragraph description of the return payload shape.",
    )
    key_fields: List[str] = Field(
        default_factory=list,
        description="The result fields the model should focus on.",
    )
    combo_hints: List[str] = Field(
        default_factory=list,
        description="Cross-tool resonance / contradiction patterns.",
    )
    cost: CostLevel = Field(
        "cheap",
        description="Rough call-cost class; helps the model budget tool calls.",
    )
    failure_modes: List[str] = Field(
        default_factory=list,
        description="Known empty-result / error patterns (e.g. non-trading-day).",
    )

    @field_validator("tool")
    @classmethod
    def _tool_non_empty(cls, value: str) -> str:
        v = value.strip()
        if not v:
            raise ValueError("tool name must be non-empty")
        return v


class DimensionManifest(BaseModel):
    """Top-level manifest file for one expert dimension."""

    dimension: str = Field(..., description="e.g. 'capital', 'sentiment', 'fundamental'.")
    description: Optional[str] = Field(
        None,
        description="Optional human-facing description of this dimension's purpose.",
    )
    tools: List[ManifestEntry] = Field(..., min_length=1)

    @field_validator("tools")
    @classmethod
    def _no_duplicate_tools(cls, value: List[ManifestEntry]) -> List[ManifestEntry]:
        seen: set[str] = set()
        for entry in value:
            if entry.tool in seen:
                raise ValueError(f"duplicate manifest entry for tool: {entry.tool}")
            seen.add(entry.tool)
        return value
