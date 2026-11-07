"""TypeSafe System One HTTP client with bounded retries and redacted errors."""

from __future__ import annotations

import math
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from src.config import Config, get_config


class JevClientError(Exception):
    """Safe, machine-readable error; never includes request headers or bodies."""


class JevClient:
    def __init__(self, config: Config | None = None, *, transport=None, sleep=time.sleep) -> None:
        self.config = config or get_config()
        self.transport = transport
        self.sleep = sleep

    def evaluate(self, state: dict, questions: dict) -> dict[str, Any]:
        if not self.config.jev_enabled or not self.config.jev_api_key:
            raise JevClientError("not_configured")
        url = urlsplit(self.config.jev_base_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise JevClientError("invalid_base_url")
        started = time.monotonic()
        try:
            with httpx.Client(timeout=self.config.jev_timeout_seconds, transport=self.transport,
                              follow_redirects=False) as client:
                for attempt in range(2):
                    response = client.post(
                        self.config.jev_base_url.rstrip("/") + "/systemone",
                        headers={"Authorization": f"Bearer {self.config.jev_api_key}"},
                        json={"model": self.config.jev_model, "state": state, "questions": questions},
                    )
                    if attempt == 0 and response.status_code in {429, 529}:
                        self.sleep(1.0)
                        continue
                    if response.status_code != 200:
                        raise JevClientError(f"http_{response.status_code}")
                    payload = response.json()
                    validate_answers(payload, questions)
                    return {
                        "model": payload["model"],
                        "answers": {key: {field: answer[field] for field in
                                          ("type", "choice", "confidence", "probabilities")}
                                    for key, answer in payload["answers"].items()},
                        "usage": {key: payload.get("usage", {}).get(key, 0)
                                  for key in ("input_tokens", "output_tokens")},
                        "latency_ms": round((time.monotonic() - started) * 1000),
                    }
        except JevClientError:
            raise
        except httpx.TimeoutException:
            raise JevClientError("timeout") from None
        except httpx.HTTPError:
            raise JevClientError("connection_error") from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise JevClientError("invalid_response") from None
        raise JevClientError("invalid_response")


def validate_answers(payload: Any, questions: dict) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str) or not payload["model"]:
        raise JevClientError("invalid_response")
    answers = payload.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise JevClientError("invalid_response")
    for key, question in questions.items():
        answer = answers[key]
        options = question["criteria"]
        if not isinstance(answer, dict) or answer.get("type") != "choice" or answer.get("choice") not in options:
            raise JevClientError("invalid_response")
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != set(options):
            raise JevClientError("invalid_response")
        values = [*probabilities.values(), answer.get("confidence")]
        if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or not 0 <= v <= 1
               for v in values):
            raise JevClientError("invalid_response")
        # The API rounds probabilities to two decimals; preserve the original values.
        if abs(sum(probabilities.values()) - 1) > 0.025:
            raise JevClientError("invalid_response")
        if probabilities[answer["choice"]] + 0.011 < max(probabilities.values()):
            raise JevClientError("invalid_response")
