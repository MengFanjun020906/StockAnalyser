# -*- coding: utf-8 -*-
"""Graphiti reranker adapters."""

from __future__ import annotations

from graphiti_core.cross_encoder.client import CrossEncoderClient


class DeterministicGraphitiReranker(CrossEncoderClient):
    """Stable fallback reranker that avoids Graphiti's implicit OpenAI dependency."""

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        total = max(len(passages), 1)
        return [
            (passage, 1.0 - (index / total))
            for index, passage in enumerate(passages)
        ]
