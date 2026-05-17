# -*- coding: utf-8 -*-
"""Score and confidence calibration helpers for evidence cards."""

from __future__ import annotations

from typing import Any


def clamp_score_delta(value: Any, *, hard_limit: float = 25.0) -> float:
    """Clamp one evidence score delta to the protocol range."""
    try:
        number = float(value)
    except Exception:
        number = 0.0
    return max(-hard_limit, min(hard_limit, number))


def clamp_confidence(value: Any) -> float:
    """Clamp confidence to 0..1."""
    try:
        number = float(value)
    except Exception:
        number = 0.0
    return max(0.0, min(1.0, number))


def strength_from_delta(delta: float) -> str:
    """Map score delta magnitude to weak/medium/strong/extreme."""
    magnitude = abs(float(delta or 0.0))
    if magnitude >= 20:
        return "extreme"
    if magnitude >= 10:
        return "strong"
    if magnitude >= 5:
        return "medium"
    return "weak"
