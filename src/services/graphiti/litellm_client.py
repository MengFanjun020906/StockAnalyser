# -*- coding: utf-8 -*-
"""Graphiti LLM adapter backed by LiteLLM."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, get_origin

import litellm
from pydantic import BaseModel, ValidationError

from graphiti_core.llm_client import LLMClient, LLMConfig
from graphiti_core.llm_client.client import DEFAULT_MAX_TOKENS
from graphiti_core.llm_client.config import ModelSize
from graphiti_core.prompts.models import Message

from src.config import get_config

logger = logging.getLogger(__name__)


def _resolve_litellm_exception(name: str) -> type[BaseException]:
    exc = getattr(litellm, name, None)
    if isinstance(exc, type) and issubclass(exc, BaseException):
        return exc

    class _FallbackLiteLLMError(Exception):
        pass

    _FallbackLiteLLMError.__name__ = f"Fallback{name}"
    return _FallbackLiteLLMError


class LiteLLMGraphitiClient(LLMClient):
    """Graphiti LLM client using the project LiteLLM config."""

    def __init__(self, config: LLMConfig | None = None, cache: bool = False):
        project_config = get_config()
        if config is None:
            model = project_config.graphiti_llm_model or project_config.litellm_model or "openai/gpt-4o-mini"
            small_model = project_config.graphiti_llm_model or (
                project_config.litellm_fallback_models[0]
                if project_config.litellm_fallback_models
                else model
            )
            config = LLMConfig(
                model=model,
                small_model=small_model,
                temperature=project_config.llm_temperature,
                max_tokens=DEFAULT_MAX_TOKENS,
            )
        super().__init__(config, cache)
        self._project_config = project_config

    def _resolve_model(self, model_size: ModelSize) -> str:
        if model_size == ModelSize.small and self.small_model:
            return self.small_model
        return self.model or "openai/gpt-4o-mini"

    async def _call_litellm(self, **kwargs: Any) -> Any:
        acompletion = getattr(litellm, "acompletion", None)
        if callable(acompletion):
            response = acompletion(**kwargs)
            if hasattr(response, "__await__"):
                return await response
            return response

        completion = getattr(litellm, "completion")
        return await asyncio.to_thread(completion, **kwargs)

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, Any]:
        model = self._resolve_model(model_size)
        payload_messages = [
            {"role": msg.role, "content": msg.content}
            for msg in messages
        ]
        request_kwargs: dict[str, Any] = {
            "model": model,
            "messages": payload_messages,
            "max_tokens": max_tokens,
            "temperature": self.temperature,
        }
        if "qwen3" in model.lower():
            request_kwargs["think"] = False
        if response_model is not None:
            schema = response_model.model_json_schema()
            schema_name = getattr(response_model, "__name__", "structured_response")
            request_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "schema": schema,
                },
            }
            payload_messages.append({
                "role": "user",
                "content": (
                    "Return only the JSON data object that matches the requested schema. "
                    "Do not return the schema itself, do not include markdown, and do not include commentary. "
                    f"Required top-level keys: {', '.join(schema.get('properties', {}).keys()) or schema_name}."
                ),
            })

        try:
            response = await self._call_litellm(**request_kwargs)
            content = response.choices[0].message.content or "{}"
            parsed = json.loads(content)
            if response_model is not None:
                try:
                    response_model(**parsed)
                except ValidationError as exc:
                    if _looks_like_json_schema(parsed):
                        fallback = _empty_response_for_model(response_model)
                        if fallback is not None:
                            logger.warning(
                                "Graphiti LLM model=%s returned a JSON schema instead of data; using empty %s fallback",
                                model,
                                getattr(response_model, "__name__", "response_model"),
                            )
                            return fallback
                    raise exc
            return parsed
        except Exception as exc:
            rate_limit_error = _resolve_litellm_exception("RateLimitError")
            if isinstance(exc, rate_limit_error):
                raise
            logger.warning("Graphiti LLM call failed for model=%s: %s", model, exc)
            raise

    async def generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int | None = None,
        model_size: ModelSize = ModelSize.medium,
        group_id: str | None = None,
        prompt_name: str | None = None,
    ) -> dict[str, Any]:
        if max_tokens is None:
            max_tokens = self.max_tokens
        return await super().generate_response(
            messages,
            response_model=response_model,
            max_tokens=max_tokens,
            model_size=model_size,
            group_id=group_id,
            prompt_name=prompt_name,
        )


def _looks_like_json_schema(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        value.get("type") == "object"
        and isinstance(value.get("properties"), dict)
        and ("$defs" in value or "title" in value)
    )


def _empty_response_for_model(response_model: type[BaseModel]) -> dict[str, Any] | None:
    fields = getattr(response_model, "model_fields", {})
    if not fields:
        return None
    payload: dict[str, Any] = {}
    for name, field in fields.items():
        annotation = getattr(field, "annotation", None)
        origin = get_origin(annotation)
        if origin is list or annotation is list:
            payload[name] = []
        elif origin is dict or annotation is dict:
            payload[name] = {}
        elif annotation is str:
            payload[name] = ""
        elif annotation is bool:
            payload[name] = False
        elif annotation in {int, float}:
            payload[name] = 0
        else:
            return None
    try:
        response_model(**payload)
    except ValidationError:
        return None
    return payload
