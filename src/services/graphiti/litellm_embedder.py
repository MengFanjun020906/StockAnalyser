# -*- coding: utf-8 -*-
"""Graphiti embedding adapter backed by LiteLLM."""

from __future__ import annotations

import hashlib
import logging
import asyncio
import math
import os
import re
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
        self.config = EmbedderConfig(
            embedding_dim=embedding_dim or int(getattr(EmbedderConfig(), "embedding_dim", 1024))
        )
        model = embedding_model or project_config.graphiti_embedding_model
        self.api_base = project_config.graphiti_embedding_base_url
        self.api_key = project_config.graphiti_embedding_api_key
        if model:
            self.model = model
            self._use_local_hash = model in {"local/hash", "local/hash-embedding"}
        elif self.api_key or _has_openai_compatible_embedding_key():
            self.model = "openai/text-embedding-3-small"
            self._use_local_hash = False
        else:
            self.model = "local/hash-embedding"
            self._use_local_hash = True
            logger.warning(
                "Graphiti embedding API key is not configured; using local hash embeddings for best-effort graph sync"
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
        if self._use_local_hash:
            return _hash_embedding(input_data, self.config.embedding_dim)

        kwargs: dict[str, Any] = {"model": self.model, "input": input_data}
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        response = await self._call_embedding(**kwargs)
        return response.data[0]["embedding"][: self.config.embedding_dim]

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        if self._use_local_hash:
            return [_hash_embedding(item, self.config.embedding_dim) for item in input_data_list]

        kwargs: dict[str, Any] = {"model": self.model, "input": input_data_list}
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        response = await self._call_embedding(**kwargs)
        return [item["embedding"][: self.config.embedding_dim] for item in response.data]


def _has_openai_compatible_embedding_key() -> bool:
    return bool(
        os.getenv("OPENAI_API_KEY")
        or os.getenv("OPENAI_API_KEYS")
        or os.getenv("AIHUBMIX_KEY")
    )


def _hash_embedding(input_data: Any, dimensions: int) -> list[float]:
    text = _embedding_text(input_data)
    vector = [0.0] * max(int(dimensions or 0), 1)
    tokens = _embedding_tokens(text)
    if not tokens:
        return vector

    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big", signed=False)
        index = value % len(vector)
        sign = 1.0 if value & 1 else -1.0
        vector[index] += sign

    norm = math.sqrt(sum(item * item for item in vector))
    if norm <= 0:
        return vector
    return [item / norm for item in vector]


def _embedding_text(input_data: Any) -> str:
    if isinstance(input_data, str):
        return input_data
    if isinstance(input_data, Iterable):
        return " ".join(str(item) for item in input_data)
    return str(input_data or "")


def _embedding_tokens(text: str) -> list[str]:
    normalized = str(text or "").lower()
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", normalized)
    cjk_text = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    cjk_bigrams = [cjk_text[index:index + 2] for index in range(max(len(cjk_text) - 1, 0))]
    return words + cjk_bigrams
