# -*- coding: utf-8 -*-
"""Model-specific semantic edge threshold profiles."""

from __future__ import annotations

import json
from typing import Any, Dict


DEFAULT_THRESHOLD_PROFILES = {
    "default": 0.78,
    "mxbai-embed-large": 0.76,
}


def resolve_semantic_threshold(
    model: str,
    *,
    profiles_json: str = "",
) -> Dict[str, Any]:
    profiles = dict(DEFAULT_THRESHOLD_PROFILES)
    if str(profiles_json or "").strip():
        try:
            loaded = json.loads(profiles_json)
            if isinstance(loaded, dict):
                for key, value in loaded.items():
                    try:
                        profiles[str(key).strip()] = float(value)
                    except (TypeError, ValueError):
                        continue
        except Exception:
            pass

    model_key = str(model or "").strip()
    matched_key = "default"
    for key in profiles:
        if key == "default":
            continue
        if model_key == key or model_key.endswith(f"/{key}") or model_key.endswith(f":{key}"):
            matched_key = key
            break
    threshold = max(0.5, min(float(profiles.get(matched_key, profiles.get("default", 0.78))), 0.95))
    return {
        "model": model_key,
        "matched_key": matched_key,
        "threshold": round(threshold, 6),
        "profile": f"embedding-threshold:{matched_key}",
        "available_profiles": sorted(profiles),
    }
