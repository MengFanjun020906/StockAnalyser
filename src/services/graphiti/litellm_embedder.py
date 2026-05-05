# -*- coding: utf-8 -*-
"""Graphiti embedding adapter backed by LiteLLM."""

from __future__ import annotations

import logging
import asyncio
from collections.abc import Iterable
from typing import Any

import litellm

from graphiti_core.embedder import EmbedderClient
from graphiti_core.embedder.client import EmbedderConfig

from src.config import get_config

logger = logging.getLogger(__name__)


class LiteLLMGraphitiEmbedder(EmbedderClient):
    """Embedder adapter that uses the project LiteLLM settings."""

    def __init__(self, embedding_model: str | None = None, embedding_dim: int | None = None):
        project_config = get_config()
        model = embedding_model or project_config.graphiti_embedding_model or "openai/text-embedding-3-small"
        self.model = model
        self.api_base = project_config.graphiti_embedding_base_url
        self.api_key = project_config.graphiti_embedding_api_key
        self.config = EmbedderConfig(
            embedding_dim=embedding_dim or int(getattr(EmbedderConfig(), "embedding_dim", 1024))
        )

    async def _call_embedding(self, **kwargs: Any) -> Any:
        aembedding = getattr(litellm, "aembedding", None)
        if callable(aembedding):
            response = aembedding(**kwargs)
            if hasattr(response, "__await__"):
                return await response
            return response

        embedding = getattr(litellm, "embedding")
        return await asyncio.to_thread(embedding, **kwargs)

    async def create(
        self,
        input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]],
    ) -> list[float]:
        kwargs: dict[str, Any] = {"model": self.model, "input": input_data}
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        response = await self._call_embedding(**kwargs)
        return response.data[0]["embedding"][: self.config.embedding_dim]

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        kwargs: dict[str, Any] = {"model": self.model, "input": input_data_list}
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        response = await self._call_embedding(**kwargs)
        return [item["embedding"][: self.config.embedding_dim] for item in response.data]
